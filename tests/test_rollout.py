"""Checks of the rollout's decisions, the parts that decide how far a bad image can reach: python
tests/test_rollout.py (make test). No BMC is needed: pre-flight and the post-check read the lab BMC as a collect saved
it, and the update itself is replaced, so what is tested is what the rollout decides, from real Redfish data."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent  # the repository, where the tools under test are
sys.path.insert(0, str(ROOT))

import poller
import rollout
from rollout import waves


# ---- The plan: waves ----

# The lab: 10 BMCs in 3 racks, 2441 marked canary: a canary, then waves of 3 (one per rack) and 6 (two per rack)
def test_lab():
    racks = ["r1", "r2", "r3", "r1", "r2", "r3", "r1", "r2", "r3", "r1"]
    servers = [{"host": f"h{n}", "rack": rack, **({"canary": True} if n == 1 else {})}
               for n, rack in enumerate(racks, 1)]
    plan = waves(servers, dict.fromkeys((s["host"] for s in servers), "- -"),
                 {"canary_per_model": 1, "waves": [33.0, 100.0], "max_per_rack": 2})
    assert [(name, [s["host"] for s in hosts]) for name, hosts in plan] == [
        ("canary", ["h1"]), ("wave 1", ["h2", "h3", "h4"]), ("wave 2", ["h5", "h6", "h7", "h8", "h9", "h10"])]


# A mixed fleet: the canary is the marked host of every model, no wave has two hosts of one rack, every host once
def test_fleet():
    servers = [{"host": f"n{i}", "rack": f"r{i % 100}", **({"canary": True} if i in (500, 501, 502) else {})}
               for i in range(1000)]
    models = {s["host"]: ("ASUS", "Dell", "HPE")[i % 3] for i, s in enumerate(servers)}
    plan = waves(servers, models, {"canary_per_model": 1, "waves": [5.0, 25.0, 100.0], "max_per_rack": 1})
    assert sorted(s["host"] for s in plan[0][1]) == ["n500", "n501", "n502"]
    assert sorted(models[s["host"]] for s in plan[0][1]) == ["ASUS", "Dell", "HPE"]
    assert all(max(Counter(s["rack"] for s in hosts).values()) == 1 for _, hosts in plan[1:])
    assert [len(hosts) for _, hosts in plan][:3] == [3, 50, 100]  # 5% of 997, then capped at 100 racks × 1
    assert sorted(s["host"] for _, hosts in plan for s in hosts) == sorted(s["host"] for s in servers)


# Canaries keep to the rack limit too: two models marked canary in one rack, at most one per rack, so the second
# model's canary is its host in another rack
def test_canary_racks():
    servers = [{"host": "a1", "rack": "r1", "canary": True}, {"host": "b1", "rack": "r1", "canary": True},
               {"host": "a2", "rack": "r2"}, {"host": "b2", "rack": "r2"}]
    models = {"a1": "A", "a2": "A", "b1": "B", "b2": "B"}
    plan = waves(servers, models, {"canary_per_model": 1, "waves": [100.0], "max_per_rack": 1})
    assert sorted(s["host"] for s in plan[0][1]) == ["a1", "b2"]


# ---- Pre-flight, the post-check, the rollback and the gate, on the lab BMC ----

# The lab BMC as a collect read it: OpenBMC's GB200 NVL build 1754, healthy, no task open
FIXTURE = json.loads((Path(__file__).parent / "testdata" / "openbmc-gb200.json").read_text())["resources"]
BMC, MODEL = "Manager (BMC)", "- -"  # the emulated GB200 reports no manufacturer or model
MGR, FW = "/redfish/v1/Managers/bmc", "/redfish/v1/UpdateService/FirmwareInventory/560e4d15"
TASK = "/redfish/v1/TaskService/Tasks/1"
RUNNING = "3.1.0-dev-1341-g16d23c4743.1790736796.7887356"  # build 1754
TARGET = "3.1.0-dev-1355-gf87a399e31.1790823238.3061957"  # build 1755
FILES = Path(tempfile.mkdtemp())


def lab() -> dict:
    return copy.deepcopy(FIXTURE)


# An image file and its catalog entry
def image(name, data=b"firmware") -> dict:
    (FILES / name).write_bytes(data)
    return {"file": str(FILES / name), "sha256": hashlib.sha256(data).hexdigest()}


# images.yaml for the lab: the target's image, and the running version's, which the rollback reinstalls
def catalog(**versions) -> dict:
    return {MODEL: {BMC: versions or {RUNNING: image("1754.tar"), TARGET: image("1755.tar")}}}


# The BMC reporting another running version
def running(found, version) -> dict:
    found[MGR]["FirmwareVersion"] = found[FW]["Version"] = version
    return found


def task(state, status) -> dict:
    return {"@odata.type": "#Task.v1_7_0.Task", "Name": "update", "TaskState": state, "TaskStatus": status}


# Pre-flight of the lab's update, the lab's policy unless told otherwise: (verdict, its reasons)
def preflight(found=None, want=TARGET, images=None, model=MODEL, **policy) -> tuple[str, str]:
    r = rollout.check(found or lab(), {model: {BMC: want}}, BMC,
                      {"allow_downgrade": True, "images": catalog() if images is None else images, **policy})
    return r["verdict"], "; ".join(r["reasons"])


# The lab's update: build 1755 approved, both images in the catalog, the BMC healthy with nothing open
def test_preflight_go():
    assert preflight() == ("go", "")


# Already on the approved version, or none approved for the model: nothing to do, a skip, not a block
def test_preflight_nothing_to_do():
    assert preflight(want=RUNNING) == ("skip", "already on the baseline version")
    assert preflight(model="Dell Inc. PowerEdge R630") == ("skip", "not in the baseline for - -")


# A downgrade, or an order nobody can tell (OpenBMC's versions follow no scheme), needs --allow-downgrade.
# Dotted integers compare as numbers: 2.9 to 2.10 is up
def test_preflight_direction():
    verdict, reason = preflight(allow_downgrade=False)
    assert verdict == "block" and "unknown" in reason and "--allow-downgrade if intended" in reason, reason
    dotted = catalog(**{"2.9.0": image("2.9.0.tar"), "2.10.0": image("2.10.0.tar")})
    assert preflight(running(lab(), "2.9.0"), "2.10.0", dotted, allow_downgrade=False) == ("go", "")
    verdict, reason = preflight(running(lab(), "2.10.0"), "2.9.0", dotted, allow_downgrade=False)
    assert verdict == "block" and "down by dotted integers" in reason, reason


# The image must be the catalog's: no file, a sha256 that doesn't match, or bigger than the BMC takes blocks
def test_preflight_image():
    back = image("1754.tar")
    verdict, reason = preflight(images=catalog(**{RUNNING: back, TARGET: {**image("1755.tar"), "sha256": "0" * 64}}))
    assert verdict == "block" and "sha256 doesn't match" in reason, reason
    verdict, reason = preflight(images=catalog(**{RUNNING: back, TARGET: {"file": str(FILES / "gone"), "sha256": "0"}}))
    assert verdict == "block" and "gone: no such file" in reason, reason
    found = lab()
    found["/redfish/v1/UpdateService"]["MaxImageSizeBytes"] = 4
    verdict, reason = preflight(found)
    assert verdict == "block" and "8 bytes, MaxImageSizeBytes 4" in reason, reason


# With a site cache, pre-flight fetches the image into the agent's copy, which it pushes, and checks it against the
# catalog: an image the cache doesn't have, or bytes that don't match, block. The cache is a real HTTP server
def test_image_cache():
    import functools
    import http.server
    import threading

    class Cache(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass
    store, spool, sha = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), hashlib.sha256(b"firmware").hexdigest()
    (store / "bmc").mkdir()
    (store / "bmc" / "1755.tar").write_bytes(b"firmware")
    (store / "bmc" / "tampered.tar").write_bytes(b"tampered")
    cache = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Cache, directory=str(store)))
    threading.Thread(target=cache.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{cache.server_port}"
    with patch.object(rollout, "IMAGE_CACHE", url), patch.object(rollout, "SPOOL", spool):
        assert rollout.image_problems("bmc/1755.tar", sha) == ()
        assert (spool / "bmc" / "1755.tar").read_bytes() == b"firmware"
        assert rollout.image_problems("bmc/gone.tar", sha)[0].startswith("bmc/gone.tar: not fetched from the image")
        assert rollout.image_problems("bmc/tampered.tar", sha) == (
            "bmc/tampered.tar: sha256 doesn't match images.yaml",)
        assert not (spool / "bmc" / "tampered.tar").exists()  # deleted: the next run fetches it again
    cache.shutdown()


# The ingest's checks, run in a copy of the repo's layout that pins a key made here: verify-fw-package.sh, the sandbox's
# script, run here without the sandbox, then promote-store-fw-package.sh, its store a fake docker that records each
# upload. A package signed with that key and built for the lab's machine is listed, uploaded and written into the
# catalog. One without its image's signature, with a changed image, without a required file, signed with another key,
# not signed at all, built for another machine, holding a link or a file that isn't a package's is refused, and with it
# every package: nothing is listed. The promotion refuses a package changed since the sandbox listed it, and a line the
# sandbox doesn't write, before the first upload
def test_signature_gate():
    import shutil
    import subprocess
    run = lambda *cmd, **kw: subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    d = Path(tempfile.mkdtemp())
    ingest = d / "control" / "ingest"
    ingest.mkdir(parents=True)
    (d / "lab").mkdir()
    for script in ("verify-fw-package.sh", "promote-store-fw-package.sh"):
        shutil.copy2(ROOT / "control" / "ingest" / script, ingest)
    for k in ("pinned", "other"):
        run("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024", "-out", d / f"{k}.key")
    run("openssl", "pkey", "-in", d / "pinned.key", "-pubout", "-out", ingest / "openbmc-dev.pub")
    (d / "docker").write_text('#!/bin/sh\ncat > /dev/null\necho "$@" >> "$0.uploads"\n')
    (d / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
    uploads = d / "docker.uploads"

    # An OpenBMC package of the build in d/: its files, the signed ones signed with key, built for machine, the image
    # changed after its signing, the files in drop left out, and the extra ones added: bytes, or a link's target
    def package(build, key="pinned", signed=("MANIFEST", "publickey", "image-bmc"), machine="gb200nvl-obmc",
                changed=b"", drop=(), extra=None) -> Path:
        manifest = (f"purpose=xyz.openbmc_project.Software.Version.VersionPurpose.BMC\nversion=2.{build}\n"
                    f"MachineName={machine}\n")
        files = {"MANIFEST": manifest.encode(), "publickey": b"key", "image-bmc": b"firmware"}
        work = Path(tempfile.mkdtemp())
        for name, data in files.items():
            (work / name).write_bytes(data)
            if name in signed:
                run("openssl", "dgst", "-sha256", "-sign", d / f"{key}.key", "-out", work / f"{name}.sig", work / name)
        (work / "image-bmc").write_bytes(b"firmware" + changed)
        for name, what in (extra or {}).items():
            (work / name).write_bytes(what) if isinstance(what, bytes) else (work / name).symlink_to(what)
        names = [n for n in (*files, *(f"{f}.sig" for f in signed), *(extra or {})) if n not in drop]
        tar = d / f"gb200nvl-{build}.static.mtd.all.tar"
        run("tar", "-cf", tar, "-C", work, *names)
        return tar
    verify = lambda *tars: run(ingest / "verify-fw-package.sh", *tars)
    listed = d / "verified"
    promote = lambda: run(ingest / "promote-store-fw-package.sh", listed, env=env)
    refusals = [  # (package, what the sandbox says)
        (package(1, signed=("MANIFEST", "publickey")), "image-bmc or its signature image-bmc.sig is missing"),
        (package(2, changed=b"!"), "image-bmc: its signature doesn't verify with openbmc-dev.pub"),
        (package(3, drop=("image-bmc",)), "image-bmc or its signature image-bmc.sig is missing"),
        (package(4, key="other"), "its signature doesn't verify with openbmc-dev.pub"),
        (package(5, signed=()), "MANIFEST or its signature MANIFEST.sig is missing"),
        (package(6, machine="romulus"), "built for romulus, not gb200nvl-obmc"),
        (package(7, extra={"image-full.sig": "/etc/passwd"}), "image-full.sig isn't a regular file"),
        (package(8, extra={"run.sh": b"#!/bin/sh"}), "holds run.sh, which isn't an OpenBMC package's"),
    ]
    for tar, says in refusals:
        refused = verify(tar)
        assert refused.returncode == 1 and says in refused.stderr and not refused.stdout, (tar.name, refused.stderr)
    refused = verify(package(9), refusals[0][0])  # one bad package: none is listed
    assert refused.returncode == 1 and not refused.stdout, refused.stdout

    passed = verify(package(9))
    assert passed.returncode == 0, passed.stderr
    sha = hashlib.sha256((d / "gb200nvl-9.static.mtd.all.tar").read_bytes()).hexdigest()
    assert passed.stdout == f"gb200nvl-9.static.mtd.all.tar {sha} 2.9\n", passed.stdout
    for line, says in [(f"gb200nvl-9.static.mtd.all.tar {'0' * 64} 2.9", "changed since the sandbox verified it"),
                       (f"gb200nvl-9.static.mtd.all.tar;reboot {sha} 2.9", "not a line the sandbox writes")]:
        listed.write_text(line + "\n")
        refused = promote()
        assert refused.returncode == 1 and says in refused.stderr, refused.stderr
    assert not uploads.exists(), "a refused package reached the store"
    listed.write_text(passed.stdout)
    promoted = promote()
    assert promoted.returncode == 0, promoted.stderr
    assert "gb200nvl-9.static.mtd.all.tar" in uploads.read_text()
    catalog = (d / "control" / "catalog" / "lab" / "images.yaml").read_text()
    assert f'"2.9": {{file: "images/gb200nvl-9.static.mtd.all.tar", sha256: "{sha}"}}' in catalog
    assert (d / "lab" / "bmc.mtd").read_bytes() == b"firmware"
    assert not (d / "gb200nvl-9.static.mtd.all.tar").exists() and not listed.exists()  # the store keeps the only copy


# No way back, no image of the running version to reinstall, blocks unless --accept-no-rollback
def test_preflight_rollback():
    no_way_back = catalog(**{TARGET: image("1755.tar")})
    verdict, reason = preflight(images=no_way_back)
    assert verdict == "block" and "--accept-no-rollback to update anyway" in reason, reason
    assert preflight(images=no_way_back, accept_no_rollback=True) == ("go", "")
    # An A/B bank the BMC reports, with a way to switch to it, isn't a way back: the rollout can't switch banks
    found = lab()
    found[MGR].setdefault("Links", {})["SoftwareImages"] = [{"@odata.id": FW}]
    with patch.object(rollout, "ab_bank", lambda found, component: "yes: Inactive 1.0"):
        verdict, reason = preflight(found, images=no_way_back)
        assert verdict == "block" and "an A/B bank is reported, but switching banks isn't built" in reason, reason
        assert preflight(found) == ("go", "")  # with the image to reinstall, the bank doesn't matter


# The BMC itself: unreachable, a component Redfish can't update, not healthy (a degraded boot), or a task
# that failed blocks; a task still running is a skip, for a later wave
def test_preflight_host():
    assert preflight({"/redfish/v1": {"error": "timed out"}}) == ("block", "no live read: timed out")
    found = lab()
    found[FW]["Updateable"] = False
    assert preflight(found) == ("block", "Redfish can't update it: Updateable false (reporting only)")
    found = lab()
    found[MGR]["Status"]["Health"] = "Critical"
    assert preflight(found) == ("block", f"{MGR}: Critical")
    found = lab()
    found[TASK] = task("Running", "OK")
    assert preflight(found) == ("skip", "1 update: Running")
    found[TASK] = task("Exception", "Critical")
    assert preflight(found) == ("block", "1 update: Exception")


# The post-check after the reset, which decides between updated and a rollback
def test_post_check():
    before = rollout.before_state(lab(), RUNNING)
    assert rollout.after_checks(running(lab(), TARGET), BMC, TARGET, before)[0] is True
    # The task said Completed, but the old firmware still runs (silent-fail)
    passed, checks, now = rollout.after_checks(lab(), BMC, TARGET, before)
    assert (passed, now, [c["check"] for c in checks if not c["ok"]]) == (False, RUNNING, ["running version"])
    # Health worse than before fails; health that was already Warning and still is doesn't
    found = running(lab(), TARGET)
    found[MGR]["Status"]["Health"] = "Warning"
    assert rollout.after_checks(found, BMC, TARGET, before)[0] is False
    assert rollout.after_checks(found, BMC, TARGET, rollout.before_state(found, RUNNING))[0] is True
    # A manager that is gone since the update fails it
    found = running(lab(), TARGET)
    del found[MGR]
    passed, checks, _ = rollout.after_checks(found, BMC, TARGET, before)
    assert not passed and f"{MGR}: gone, before OK" in [c["detail"] for c in checks if not c["ok"]], checks
    # A task that failed since the update fails it; one already open before the update doesn't
    found = running(lab(), TARGET)
    found[TASK] = task("Exception", "Critical")
    assert rollout.after_checks(found, BMC, TARGET, before)[0] is False
    assert rollout.after_checks(found, BMC, TARGET, rollout.before_state(found, RUNNING))[0] is True


# How the running version is known, marked on the post-check: self-reported, as on the lab BMC, which offers no
# ComponentIntegrity, unless an enabled SPDM one targets or protects the component: its firmware inventory entry, or the
# manager it belongs to. One for another component, a TPM one, or a disabled one leaves it self-reported. The poller's
# capabilities count each type a model offers
def test_attestation():
    def version_detail(found):
        checks = rollout.after_checks(found, BMC, TARGET, rollout.before_state(lab(), RUNNING))[1]
        return next(c["detail"] for c in checks if c["check"] == "running version")
    def integrity(n, kind="SPDM", target=None, protects=(), enabled=True):
        return {"@odata.type": "#ComponentIntegrity.v1_3_0.ComponentIntegrity", "Id": n, "ComponentIntegrityType": kind,
                "ComponentIntegrityEnabled": enabled, "TargetComponentURI": target,
                "Links": {"ComponentsProtected": [{"@odata.id": u} for u in protects]}}
    ci = "/redfish/v1/ComponentIntegrity"
    assert version_detail(running(lab(), TARGET)) == f"{TARGET}, target {TARGET} · self-reported"
    for offered in ({f"{ci}/GPU_0": integrity("GPU_0", target="/redfish/v1/Chassis/HGX_GPU_0")},
                    {f"{ci}/TPM": integrity("TPM", kind="TPM", target=FW)},
                    {f"{ci}/off": integrity("off", target=FW, enabled=False)}):
        assert version_detail({**running(lab(), TARGET), **offered}).endswith("· self-reported"), offered
    for offered in ({f"{ci}/BMC_0": integrity("BMC_0", target=FW)},
                    {f"{ci}/BMC_0": integrity("BMC_0", target="/redfish/v1/Chassis/x", protects=[MGR])}):
        assert version_detail({**running(lab(), TARGET), **offered}).endswith(
            f"· SPDM offered ({ci}/BMC_0), not verified"), offered
    # The exporter: one sample per ComponentIntegrity, with the BMC's own verdict on the part's identity; none without
    assert not [s for s in poller.exposition({"host": "h"}, lab(), 1.0) if s.startswith("redfish_component_integrity")]
    offered = integrity("GPU_0", target="/redfish/v1/Chassis/HGX_GPU_0", protects=[MGR])
    offered.update(ComponentIntegrityTypeVersion="1.1.0", SPDM={"IdentityAuthentication": {"VerificationStatus": "Success"}})
    out = [s for s in poller.exposition({"host": "h"}, {**lab(), f"{ci}/GPU_0": offered}, 1.0)
           if s.startswith("redfish_component_integrity")]
    assert out == [f'redfish_component_integrity{{host="h",resource="{ci}/GPU_0",id="GPU_0",type="SPDM",'
                   f'type_version="1.1.0",target="/redfish/v1/Chassis/HGX_GPU_0",protects="{MGR}",enabled="true",'
                   f'identity="Success"}} 1'], out
    snap = {"host": "h", "resources": lab()}
    assert poller.capabilities(("-", "-"), [snap])[-1] == "-"
    snap["resources"].update({f"{ci}/GPU_{i}": integrity(f"GPU_{i}") for i in range(2)},
                             **{f"{ci}/TPM": integrity("TPM", kind="TPM")}, **{f"{ci}/off": integrity("off", enabled=False)})
    assert poller.capabilities(("-", "-"), [snap])[-1] == "SPDM 2 · TPM 1"


# One host through the update, the network replaced: first and rollback are what the update and the rollback's
# reinstall end with, (passed, activated: the reset ran, detail). (state, the images installed, the steps recorded)
def run_host(first, rollback=None, images=None, **policy) -> tuple[str, list, list]:
    installed, steps, results = [], [], [first, rollback]

    def apply(server, found, key, image, want, before, policy, rec, phase) -> tuple:
        installed.append((phase, Path(image["file"]).name, want))
        return results.pop(0)
    policy = {"allow_downgrade": True, "accept_no_rollback": False, "images": images or catalog(), "faults": {},
              "drain": None, "undrain": None, **policy}
    with (patch.object(rollout, "read_host", lambda server, link: (lab(), 0.0)),
          patch.object(rollout, "tunnel", lambda server: None), patch.object(rollout, "apply", apply)):
        out = rollout.run_host({"host": "127.0.0.1:2441"}, BMC, {MODEL: {BMC: TARGET}}, policy,
                               lambda host, step, state, **detail: steps.append((step, state)))
    return out["state"], installed, steps


def test_run_host():
    # Passed: updated. A BMC reset leaves the host running, so nothing was drained
    state, installed, steps = run_host((True, True, "post-check passed"))
    assert (state, installed) == ("updated", [("", "1755.tar", TARGET)]) and ("drain", "skipped") in steps
    # Failed before the reset: the old firmware still runs, nothing to roll back
    state, installed, steps = run_host((False, False, "update Exception: aborted"))
    assert (state, installed) == ("failed", [("", "1755.tar", TARGET)]) and ("rollback", "start") not in steps
    # Failed after the reset: reinstall the version that ran before, not the target, and check it again
    state, installed, _ = run_host((False, True, "post-check failed"), (True, True, "post-check passed"))
    assert (state, installed) == ("rolled_back", [("", "1755.tar", TARGET), ("rollback ", "1754.tar", RUNNING)])
    # The rollback fails too, or there was no way back: left for a person
    assert run_host((False, True, "post-check failed"), (False, True, "post-check failed"))[0] == "needs_attention"
    state, installed, _ = run_host((False, True, "post-check failed"), images=catalog(**{TARGET: image("1755.tar")}),
                                   accept_no_rollback=True)
    assert (state, installed) == ("needs_attention", [("", "1755.tar", TARGET)])


# A reset that restarts the host needs the scheduler: pre-flight blocks without a drain in policy.yaml. A drain that
# fails, or runs past its timeout, fails the host before any update; an undrain that fails leaves it for a person, as
# the host isn't back in service
def test_drain():
    host_reset = lambda found, component: ("ComputerSystem", "/redfish/v1/Systems/system")
    with patch.object(rollout, "activation", host_reset):
        verdict, reason = preflight()
        assert verdict == "block" and "restarts the host, and policy.yaml sets no drain" in reason, reason
        assert preflight(drain="true") == ("go", "")
        state, installed, steps = run_host((True, True, "post-check passed"), drain="false")
        assert (state, installed, ("drain", "failed") in steps) == ("failed", [], True)
        state, _, steps = run_host((True, True, "post-check passed"), drain="true", undrain="false")
        assert (state, ("undrain", "failed") in steps) == ("needs_attention", True)
        assert run_host((True, True, "post-check passed"), drain="true", undrain="true")[0] == "updated"
    with patch.object(rollout, "SCHEDULER_TIMEOUT", 0.2):
        ok, output = rollout.scheduler("sleep 5", {"host": "h"})
        assert not ok and "timed out" in output, output


# A run does what its plan approved: an input file changed or gone since the plan stops it, as does a host whose
# inventory entry changed or is gone (NetBox has no file to pin), and a host whose BMC isn't the one the plan read
# (another identity at its address) is blocked, not updated
def test_frozen_plan():
    entry = {"host": "127.0.0.1:2441", "rack": "r1"}
    assert rollout.changed_hosts({"servers": {entry["host"]: entry}}, [entry], [entry["host"]]) == []
    assert rollout.changed_hosts({"servers": {entry["host"]: entry}}, [{**entry, "rack": "r2"}], [entry["host"]]) == [
        entry["host"]]
    assert rollout.changed_hosts({"servers": {entry["host"]: entry}}, [], [entry["host"]]) == [entry["host"]]
    d = Path(tempfile.mkdtemp())
    for name in ("baseline.yaml", "images.yaml"):
        (d / name).write_text("approved\n")
    saved = {"inputs": {str(d / n): rollout.sha256_of(d / n) for n in ("baseline.yaml", "images.yaml")}}
    assert rollout.changed_inputs(saved) == []
    (d / "baseline.yaml").write_text("changed\n")
    (d / "images.yaml").unlink()
    assert rollout.changed_inputs(saved) == [str(d / "baseline.yaml"), str(d / "images.yaml")]
    approved = rollout.identity_of(lab())
    assert run_host((True, True, "post-check passed"), identities={"127.0.0.1:2441": approved})[0] == "updated"
    state, installed, _ = run_host((True, True, "post-check passed"),
                                   identities={"127.0.0.1:2441": {**approved, "service_uuid": "another BMC"}})
    assert (state, installed) == ("blocked", [])


# The run side of "only a tag makes a host writable": run --yes refuses a planned host that NetBox doesn't tag
# writable, before the state database, any lock or any update
def test_run_writable():
    server, found = {"host": "127.0.0.1:2441", "rack": "r1"}, lab()  # NetBox's entry, without the tag
    policy = {"allow_downgrade": True, "accept_no_rollback": False, "images": catalog(), "drain": None}
    result = rollout.check(found, {MODEL: {BMC: TARGET}}, BMC, policy)
    plan = Path(tempfile.mkdtemp()) / "plan.json"
    plan.write_text(json.dumps({
        "component": BMC, "baseline": "-", "images": "-", "allow_downgrade": True, "accept_no_rollback": False,
        "inputs": {}, "waves": {"canary": [server["host"]]}, "servers": {server["host"]: server},
        "rollout": {**rollout.ROLLOUT_DEFAULTS, "file": None},
        "verdicts": {server["host"]: {"identity": result.get("identity")}}}))
    step = {"servers": [server], "reads": [(found, 0.0)], "results": [result], "baselines": {}, "policy": policy,
            "read_at": "00:00:00 UTC"}

    def reached():
        raise AssertionError("run --yes reached the state database: the untagged host would be locked and updated")
    with (patch.object(rollout, "load_servers", lambda: [server]), patch.object(rollout, "prepare", lambda args: step),
          patch.object(rollout, "state_db", reached)):
        try:
            rollout.run(SimpleNamespace(plan=str(plan), stage="canary", yes=True, fault=None, reset_timeout=900))
        except SystemExit as e:
            assert "not tagged writable in NetBox: 127.0.0.1:2441" in str(e), e
        else:
            raise AssertionError("run --yes went on with a host NetBox doesn't tag writable")


# The gate after each wave, the lab's policy: the canary and wave 1 strict, later waves halt over 10% failed.
# states: every host tried so far, every wave up to this one
def test_gate():
    policy = {"strict_waves": 1, "failure_rate": 0.1}
    ok = ["updated"]
    cases = [  # (wave, states, soak failures) → halted
        (("canary", ["failed"], 0), True),
        (("canary", ok, 0), False),
        (("wave 1", ok * 10 + ["rolled_back"], 0), True),  # strict: one rollback halts, 1 of 11 or not
        (("wave 2", ok * 10 + ["rolled_back"], 0), False),  # the same after the strict waves: under 10%
        (("wave 2", ok * 9 + ["failed"], 0), False),  # 1 of 10 is 10%, not over it
        (("wave 2", ok * 8 + ["failed", "needs_attention"], 0), True),  # 2 of 10
        (("wave 2", ok * 9 + ["needs_attention"], 1), True),  # a host that failed its soak halts any wave
        (("wave 2", ["blocked"] * 10, 0), False),  # nothing tried, nothing failed
    ]
    for (wave, states, soured), halted in cases:
        assert rollout.gate(wave, states, soured, policy)["halted"] is halted, (wave, states, soured)
    # Blocked and skipped hosts were never tried: 1 failed of 2 tried is 50%
    assert rollout.gate("wave 2", ["updated", "failed", "blocked", "skipped"], 0, policy) == {
        "halted": True, "failed": 1, "tried": 2, "strict": False}


# A lock per host in the state database: a stage takes the locks of all its hosts or of none, so two pipelines never
# touch one host; at its end it gives them back, but a host left for a person keeps its lock. Events are append-only to
# the rollout's account, and a host's state is its latest. Needs the state database (make control-up), whose STATE_DB
# make test sets: skipped without it
def test_host_locks():
    try:
        db = rollout.state_db()
    except SystemExit as e:
        return str(e)
    a, b, h1, h2, h3 = (f"test-{os.getpid()}-{n}" for n in ("a", "b", "h1", "h2", "h3"))
    try:
        assert rollout.lock(db, a, "canary", [h1, h2]) == []
        assert rollout.lock(db, b, "canary", [h3, h2]) == [(h2, a)]  # h2 is a's: b takes none, not even h3
        assert rollout.lock(db, b, "canary", [h3]) == []
        # a ends: h1 updated gives its lock back, h2 left for a person keeps it; h3 is b's, not a's to give back
        outcomes = [{"host": h1, "state": "updated"}, {"host": h2, "state": "needs_attention"}]
        assert rollout.release(db, a, [h1, h2, h3], outcomes) == [h2]
        assert rollout.lock(db, b, "waves", [h1]) == []
        assert rollout.lock(db, b, "waves", [h2, h3]) == [(h2, a), (h3, b)]  # a lock is held, even by its own pipeline
        event = {"pipeline": a, "host": h1, "stage": "canary", "wave": "canary", "step": "update", "state": "start",
                 "at": "2026-10-14T10:00:00+00:00", "task": {"messages": []}}
        with db.transaction():  # rolled back: a test's events never reach the history
            rollout.save_event(db, event, {})
            rollout.save_event(db, {**event, "step": "done", "state": "updated", "detail": "post-check passed"}, {})
            assert db.execute("SELECT step, state, detail FROM host_state WHERE pipeline = %s AND host = %s",
                              (a, h1)).fetchall() == [("done", "updated", "post-check passed")]
            assert db.execute("SELECT count(*) FROM events WHERE pipeline = %s", (a,)).fetchone() == (2,)
            raise rollout.psycopg.Rollback
        for change in ("UPDATE events SET state = 'updated'", "DELETE FROM events"):
            try:
                db.execute(f"{change} WHERE pipeline = %s", (a,))
                raise AssertionError(f"the rollout's account may {change.split()[0]} events")
            except rollout.psycopg.errors.InsufficientPrivilege:
                pass
    finally:
        db.execute("DELETE FROM host_locks WHERE pipeline = ANY(%s)", ([a, b],))


# make cancel: a pipeline asked to stop starts no wave after the one under way. Its report says CANCELLED and who asked,
# the waves it never started didn't run, its metrics say so; and a soak under way ends at once, re-checking nothing
def test_cancel():
    at = lambda s: f"2026-10-15T10:00:{s}+00:00"
    why = "cancelled before wave 1, by cvag@lab at 2026-10-15T10:00:04Z"
    events = [
        {"at": at("00"), "host": "-", "step": "run", "state": "start", "waves": {"canary": ["h1"], "wave 1": ["h2"]},
         "failure_rate": 0.1},
        {"at": at("01"), "host": "h1", "step": "pre-flight", "state": "go", "running": "1.0", "target": "2.0"},
        {"at": at("02"), "host": "h1", "step": "done", "state": "updated", "running": "2.0", "target": "2.0",
         "before": "1.0"},
        {"at": at("03"), "host": "-", "step": "gate", "state": "passed", "wave": "canary", "failed": 0, "tried": 1,
         "limit": 0.1, "strict": True, "soak_failed": 0},
        {"at": at("04"), "host": "-", "step": "run", "state": "cancelled", "wave": "wave 1", "detail": why},
        {"at": at("05"), "host": "-", "step": "run", "state": "end", "detail": why}]
    doc = rollout.pipeline_report(None, events, "p", [])
    assert (doc["result"], doc["cancelled"]) == ("cancelled", why), doc["result"]
    wave1 = doc["waves"][1]
    assert wave1["gate"]["result"] == "not run" and wave1["hosts"][0]["why"] == "cancelled: its wave never started"
    text = rollout.metrics(doc)
    assert "rollout_cancelled 1" in text and "rollout_running 0" in text
    assert any("CANCELLED" in str(part) for part in rollout.render(doc))

    def recheck(*args):
        raise AssertionError("a cancelled soak re-checked a host")
    with patch.object(rollout, "post_check", recheck):
        started = time.monotonic()
        assert rollout.soak([{"state": "updated"}], BMC, 600, lambda *a, **k: None, cancelled=lambda: True) == []
        assert time.monotonic() - started < 1


# Who: ROLLOUT_ACTOR when what started the run sets it, else the account sudo came from, else the account, at the host.
# The commit a catalog file was approved in, by whom, and whether it changed since; nothing for a file git doesn't track
def test_who():
    import subprocess
    with patch.dict(os.environ, {"ROLLOUT_ACTOR": "github:cvag"}):
        assert rollout.actor() == "github:cvag"
    with patch.dict(os.environ, {"ROLLOUT_ACTOR": "", "SUDO_USER": "alice"}):
        assert rollout.actor().startswith("alice@")
    repo = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=Reviewer", "-c", "user.email=r@example.com",
                                     "-c", "commit.gpgsign=false", *a], capture_output=True, check=True)
    git("init", "-q")
    (repo / "baseline.yaml").write_text("approved\n")
    git("add", "baseline.yaml")
    git("commit", "-qm", "approve")
    approved = rollout.approval(repo / "baseline.yaml", repo)
    assert (approved["author"], approved["modified"], len(approved["commit"])) == ("Reviewer", False, 40), approved
    (repo / "baseline.yaml").write_text("changed\n")
    assert rollout.approval(repo / "baseline.yaml", repo)["modified"] is True
    (repo / "images.yaml").write_text("not in git\n")
    assert rollout.approval(repo / "images.yaml", repo) is None


# NetBox's devices as the tools' servers, page after page: a device's config context gives how its BMC is reached
# (else HTTPS, the certificate checked: every entry says, as each reader takes it), canary and writable come from its
# tags only; a device without an out-of-band IP, a usable BMC account (X_USERNAME and X_PASSWORD, set here: not any
# other of the tool's secrets) or HTTPS isn't read. No token, a site with no host, NetBox down or answering something
# else than JSON stops collect and plan with why
def test_netbox_inventory():
    lab = {"verify": False, "retries": 1, "deadline": 300, "username_env": "LAB_USERNAME",
           "password_env": "LAB_PASSWORD"}
    device = {"display": "bmc1", "name": "bmc1", "oob_ip": {"address": "127.1.0.1/32"}, "tenant": {"name": "lab"},
              "device_type": {"manufacturer": {"name": "NVIDIA"}}, "rack": {"name": "r1"},
              "tags": [{"slug": "canary"}, {"slug": "writable"}], "config_context": lab}
    # a platform's context names its account; its writable and workers are no connection keys: dropped
    bare = {**device, "display": "10.0.0.5", "name": "10.0.0.5", "oob_ip": {"address": "10.0.0.5/32"},
            "tenant": None, "rack": None, "tags": [], "device_type": {"manufacturer": {"name": "DELL"}},
            "config_context": {"username_env": "DELL_USERNAME", "password_env": "DELL_PASSWORD", "writable": True,
                               "workers": 4}}
    pages = {"http://nb/api/dcim/devices/?site=lab&limit=1000": {"results": [device], "next": "http://nb/2"},
             "http://nb/2": {"results": [bare, {**device, "display": "spare", "oob_ip": None},
                                         {**device, "display": "no account", "config_context": {"verify": False}},
                                         {**device, "display": "foreign", "config_context": {
                                             "username_env": "LAB_USERNAME", "password_env": "STATE_DB"}},
                                         {**device, "display": "unset", "config_context": {
                                             "username_env": "GONE_USERNAME", "password_env": "GONE_PASSWORD"}},
                                         {**device, "display": "plain", "config_context": {**lab, "scheme": "http"}}],
                             "next": None}}
    accounts = {"LAB_USERNAME": "u", "LAB_PASSWORD": "p", "DELL_USERNAME": "u", "DELL_PASSWORD": "p", "STATE_DB": "s"}

    class Answer:  # one page of NetBox's answer
        def __init__(self, url, **_):
            self.body = pages[url]

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    with patch.dict(os.environ, {"NETBOX_TOKEN": "nbt_key.token", **accounts}), patch("poller.requests.get", Answer):
        assert poller.load_servers("http://nb/", "lab") == [
            {"host": "127.1.0.1", "vendor": "NVIDIA", "project": "lab", "rack": "r1", "canary": True,
             "writable": True, "node": "bmc1", "scheme": "https", **lab},
            {"host": "10.0.0.5", "vendor": "DELL", "scheme": "https", "verify": True, "username_env": "DELL_USERNAME",
             "password_env": "DELL_PASSWORD"}]
    with patch.dict(os.environ, {"NETBOX_TOKEN": ""}):
        try:
            poller.load_servers("http://nb/", "lab")
        except SystemExit as e:
            assert ".tokens.env" in str(e), e
        else:
            raise AssertionError("NetBox read without a token")

    class Html(Answer):  # NETBOX_URL at something else than NetBox: a page, not JSON
        def json(self):
            raise poller.requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)

    def down(url, **_):
        raise poller.requests.ConnectionError("[Errno 111] Connection refused")
    for get in (Html, down):
        with patch.dict(os.environ, {"NETBOX_TOKEN": "nbt_key.token"}), patch("poller.requests.get", get):
            try:
                poller.load_servers("http://nb/", "lab")
            except SystemExit as e:
                assert str(e).startswith("NetBox at http://nb/:") and "make control-up" in str(e), e
            else:
                raise AssertionError(f"{get.__name__}: no answer from NetBox, yet no stop")
    with patch("poller.load_servers", lambda: []):
        try:
            poller.collect(None)
        except SystemExit as e:
            assert "no BMC in NetBox site" in str(e), e
        else:
            raise AssertionError("a site with no host collected")
    with patch("rollout.load_servers", lambda: []):
        try:
            rollout.prepare(SimpleNamespace(hosts=None))
        except SystemExit as e:
            assert "no host of NetBox site" in str(e), e
        else:
            raise AssertionError("a site with no host planned")


# ---- Update methods, metrics, the exporter ----

# Push or pull, against what the BMC allows: a target or a URL scheme outside AllowableValues means no, before any drain
def test_ways_in():
    import poller
    real = poller.update_targets_of  # ways_in looks it up in poller
    poller.update_targets_of = lambda found, component: ["/redfish/v1/Managers/bmc"]
    found = {"/redfish/v1/UpdateService": {
        "@odata.type": "#UpdateService.v1_11_0.UpdateService", "MultipartHttpPushUri": "/redfish/v1/UpdateService/up",
        "Actions": {"#UpdateService.SimpleUpdate": {
            "target": "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
            "TransferProtocol@Redfish.AllowableValues": ["HTTPS"],
            "Targets@Redfish.AllowableValues": ["/redfish/v1/Managers/bmc"]}}}}
    push, pull = poller.ways_in(found, "Manager (BMC)", "https://mirror/bmc.tar")
    assert push == "yes · /redfish/v1/UpdateService/up" and pull.startswith("yes · HTTPS")
    assert poller.ways_in(found, "Manager (BMC)", "http://mirror/bmc.tar")[1].startswith("no: HTTP not in")
    poller.update_targets_of = lambda found, component: ["/redfish/v1/Systems/system/Bios"]
    assert "not in Targets@AllowableValues" in poller.ways_in(found, "System BIOS")[1]
    poller.update_targets_of = real


# Metrics are built mid-run, when a host can have a failed update task and no end yet: that must not raise
def test_metrics_mid_run():
    at = lambda s: f"2026-10-02T18:{s}+00:00"
    events = [
        {"at": at("00:00"), "host": "-", "step": "run", "state": "start", "waves": {"wave 1": ["h1"]},
         "failure_rate": 0.1},
        {"at": at("00:01"), "host": "h1", "step": "pre-flight", "state": "go", "running": "1.0", "target": "2.0"},
        {"at": at("00:02"), "host": "h1", "step": "update", "state": "start", "method": "push"},
        {"at": at("00:09"), "host": "h1", "step": "update", "state": "Exception", "task": {
            "uri": "/redfish/v1/TaskService/Tasks/0", "state": "Exception", "messages": [
                {"severity": "Critical", "message_id": "TaskEvent.1.0.TaskAborted", "message": "aborted"}]}}]
    doc = rollout.pipeline_report(None, events, "run", [])
    assert doc["result"] == "running" and doc["waves"][0]["hosts"][0]["state"] == "running"
    text = rollout.metrics(doc)
    assert 'rollout_hosts{wave="wave 1",state="running"} 1' in text and "rollout_running 1" in text
    # Pushed again at the end: no series of the push before vanishes, so the last value in a time range is the run as
    # it stands; a host's info rows, whose labels change, rank by the time of its latest event
    end = rollout.metrics(rollout.pipeline_report(None, events + [
        {"at": at("00:10"), "host": "h1", "step": "done", "state": "failed", "detail": "aborted"},
        {"at": at("00:10"), "host": "-", "step": "run", "state": "end"}], "run", []))
    series = lambda t: {l.rsplit(" ", 1)[0] for l in t.splitlines() if l and not l.startswith("rollout_host_info")}
    assert series(text) <= series(end), series(text) - series(end)
    assert 'rollout_hosts{wave="wave 1",state="running"} 0' in end and "rollout_running 0" in end
    info = lambda t: float(next(l for l in t.splitlines() if l.startswith("rollout_host_info")).rsplit(" ", 1)[1])
    assert info(end) > info(text) > 0
    # Pushed mid-run, a host the run hasn't reached yet takes its versions from the plan, not "-"
    record = Path(tempfile.mkdtemp()) / "run.jsonl"
    record.write_text("".join(json.dumps(e) + "\n" for e in events))
    plan = {"waves": {"wave 1": ["h1"], "wave 2": ["h2"]}, "planned": 10,
            "verdicts": {h: {"verdict": "go", "running": "1.0", "target": "2.0"} for h in ("h1", "h2")}}
    pushed = []

    class Pushgateway:
        def raise_for_status(self) -> None:
            pass
    with (patch.dict(os.environ, {"PUSHGATEWAY": "http://pushgateway"}),
          patch.object(rollout.requests, "put", lambda url, data, timeout: pushed.append(data) or Pushgateway())):
        rollout.push_metrics(record, "site/lab", {}, plan)
    assert 'host="h2",state="untouched",before="1.0",target="2.0",after="-"' in pushed[0], pushed
    # The timeline's value: h1 in its update step, h2 not reached
    assert 'rollout_host_step{wave="wave 1",host="h1"} 3' in pushed[0], pushed
    assert 'rollout_host_step{wave="wave 2",host="h2"} 0' in pushed[0], pushed
    assert "rollout_hosts_drained 0" in pushed[0] and "rollout_seconds 9" in pushed[0], pushed
    assert "rollout_planned_hosts 10" in pushed[0], pushed  # the whole plan, even if this stage is its canary
    # A host drained and not given back is out of service: it counts until its undrain is done
    drained = [*events, {"at": at("00:10"), "host": "h1", "step": "drain", "state": "done"}]
    assert "rollout_hosts_drained 1" in rollout.metrics(rollout.pipeline_report(None, drained, "run", []))
    given_back = [*drained, {"at": at("00:11"), "host": "h1", "step": "undrain", "state": "done"}]
    assert "rollout_hosts_drained 0" in rollout.metrics(rollout.pipeline_report(None, given_back, "run", []))


# Each event goes to Loki as one JSON line, labelled only job and site: the pipeline, wave and host are in the line
def test_events_to_loki():
    sent = []

    class Loki:
        def raise_for_status(self) -> None:
            pass
    event = {"at": "2026-10-06T00:00:00+00:00", "host": "h1", "step": "done", "state": "failed", "pipeline": "p",
             "wave": "wave 1"}
    with (patch.dict(os.environ, {"LOKI": "http://loki"}),
          patch.object(rollout.requests, "post", lambda url, json, timeout: sent.append((url, json)) or Loki())):
        rollout.push_event(event, {})
    url, body = sent[0]
    stream = body["streams"][0]
    assert url == "http://loki/loki/api/v1/push" and stream["stream"] == {"job": "rollout", "site": rollout.SITE.name}
    assert json.loads(stream["values"][0][1]) == {**event, "level": "error"}  # a failed host, coloured as an error


# The saved plan goes to Loki as the policy, then a verdict per host with its wave: the dashboard's Plan section
def test_plan_to_loki():
    sent = []

    class Loki:
        def raise_for_status(self) -> None:
            pass
    args = type("Args", (), {"save": "lab/runs/pipeline-p/plan.json"})()
    servers = [{"host": "h1"}, {"host": "h2"}]
    results = [{"verdict": "go", "running": "1.0", "want": "2.0", "reasons": [], "component": BMC,
                "rollback": "reinstall 1.0 from bmc/1.0.tar", "ab": "not reported: one image",
                "checks": [{"check": "image sha256", "ok": True, "detail": "matches"}]},
               {"verdict": "block", "running": "1.0", "want": "2.0", "reasons": ["no image"], "component": BMC,
                "rollback": "none", "ab": "not reported: one image",
                "checks": [{"check": "image in the catalog", "ok": False, "detail": "no image"}]}]
    with (patch.dict(os.environ, {"LOKI": "http://loki"}),
          patch.object(rollout.requests, "post", lambda url, json, timeout: sent.append(json) or Loki())):
        rollout.push_plan(args, servers, [(lab(), 0.0)] * 2, results, [("canary", [servers[0]])],
                          {"canary_per_model": 1, "waves": [100.0], "max_per_rack": 0, "strict_waves": 1,
                           "failure_rate": 0.1, "max_parallel": 0, "soak": 0})
    events = [json.loads(b["streams"][0]["values"][0][1]) for b in sent]
    assert [(e["host"], e["step"], e["state"], e.get("wave")) for e in events] == [
        ("-", "plan", "saved", None), ("h1", "plan", "go", "canary"), ("h1", "pre-flight check", "passed", "canary"),
        ("h2", "plan", "block", None), ("h2", "pre-flight check", "failed", None)], events
    assert (events[4]["check"], events[4]["detail"], events[4]["stage"]) == ("image in the catalog", "no image", "plan")
    assert events[0]["pipeline"] == "pipeline-p" and events[3]["detail"] == "no image" and events[3]["level"] == "error"
    # how the lab BMC's update activates, and how it would be rolled back: the BMC alone resets, the host runs on
    assert (events[1]["activation"], events[1]["host_restarts"], events[1]["rollback"]) == (
        "Manager.Reset GracefulRestart", False, "reinstall 1.0 from bmc/1.0.tar"), events[1]


# A plan that blocks every host stops the pipeline before any stage: it still ends in Loki and pushes its blocked hosts
# and the halt, so the dashboard lists it (its pipeline list reads the Pushgateway) and shows it HALTED, blocked
def test_blocked_plan_to_grafana():
    sent, pushed = [], []

    class Ok:
        def raise_for_status(self) -> None:
            pass
    args = type("Args", (), {"save": "lab/runs/pipeline-p/plan.json"})()
    with (patch.dict(os.environ, {"LOKI": "http://loki", "PUSHGATEWAY": "http://pushgateway"}),
          patch.object(rollout.requests, "post", lambda url, json, timeout: sent.append(json) or Ok()),
          patch.object(rollout.requests, "put", lambda url, data, timeout: pushed.append((url, data)) or Ok())):
        rollout.push_blocked(args, 10, "every host to update is blocked")
    end = json.loads(sent[0]["streams"][0]["values"][0][1])
    assert (end["pipeline"], end["stage"], end["step"], end["state"]) == ("pipeline-p", "plan", "run", "end"), end
    url, body = pushed[0]
    assert url == f"http://pushgateway/metrics/job/rollout/site/{rollout.SITE.name}/pipeline/pipeline-p/stage/plan"
    assert 'rollout_hosts{wave="plan",state="blocked"} 10' in body and 'rollout_gate_halted{wave="plan"} 1' in body
    assert f'rollout_scenario{{scenario="{rollout.SCENARIO}"}} 1' in body  # which bad update, for the dashboard


# The exporter's samples are one series each: two sensors of one name (iLO 4's power supplies) stay two series.
# Thresholds take DMTF's Sensor names and their reading's unit, a Fan's too (its schema gives them none)
def test_exposition():
    import poller
    poller.schema_file = lambda name: {}  # offline: the unit comes from ReadingUnits
    # lambda n: the n-th sensor, every one named PSU
    sensor = lambda n: {"@odata.type": "#Sensor.v1_2_0.Sensor", "Name": "PSU", "Reading": 30 + n, "ReadingUnits": "Cel",
                        "PhysicalContext": "PowerSupply", "Thresholds": {"UpperCritical": {"Reading": 90}},
                        "Status": {"Health": "Warning"}}
    fan = {"Name": "Fan1", "Reading": 5000, "ReadingUnits": "RPM", "LowerThresholdNonCritical": 600,
           "UpperThresholdCritical": 0}  # an upper 0: no threshold set (iLO 4)
    absent = {"@odata.type": "#Sensor.v1_2_0.Sensor", "Name": "PCI 1", "Reading": 0, "ReadingUnits": "Cel",
              "Status": {"State": "Absent"}}  # a part not there (iLO 4 reports 0 °C): no reading
    found = {poller.ROOT: {}, **{f"/redfish/v1/Chassis/1/Sensors/{n}": sensor(n) for n in (1, 2)},
             "/redfish/v1/Chassis/1/Sensors/absent": absent,
             "/redfish/v1/Chassis/1/Thermal": {"@odata.type": "#Thermal.v1_7_0.Thermal", "Fans": [fan]}}
    out = poller.exposition({"host": "h"}, found, 1.0)
    series = [line.rsplit(" ", 1)[0] for line in out]
    assert len(series) == len(set(series)) and sum(s.startswith("redfish_reading{") for s in series) == 3, series
    thresholds = sorted(line for line in out if line.startswith("redfish_reading_threshold"))
    assert len(thresholds) == 3 and all('threshold="UpperCritical"} 90' in t for t in thresholds[1:]), thresholds
    assert 'sensor="Fan1",property="Reading",unit="RPM",threshold="LowerCaution"} 600' in thresholds[0], thresholds
    # each reading's PhysicalContext as its context, the dashboards' component panels; '-' when it reports none
    assert all('kind="Temperature",context="PowerSupply"' in t for t in thresholds[1:]), thresholds
    assert 'context="-"' in thresholds[0], thresholds
    assert sum(line.startswith("redfish_health{") and 'health="Warning"' in line for line in out) == 2
    crossed = sorted(line.rsplit(" ", 1)[1] for line in out if line.startswith("redfish_reading_crossed"))
    assert crossed == ["0", "0", "0"], crossed  # the sensors under their 90, the fan over its 600
    fan["Reading"] = 500  # under its lower caution
    assert any(line.startswith("redfish_reading_crossed{") and 'sensor="Fan1"' in line and line.endswith(" 1")
               for line in poller.exposition({"host": "h"}, found, 1.0))


# The firmware inventory's A/B bank, as the rollout's plan reads it: the lab BMC reports one image; a second, inactive
# one in the Manager's SoftwareImages is a bank, but not an empty slot (a Supermicro's Backup_BMC: "Not Present")
def test_firmware_info_ab_bank():
    import poller
    bmc = lambda found: next(line for line in poller.exposition({"host": "h"}, found, 1.0)
                             if line.startswith("redfish_firmware_info") and f'component="{BMC}"' in line)
    found = lab()
    assert 'ab_bank="not reported: one image"' in bmc(found), bmc(found)
    found[f"{FW}b"] = {**found[FW], "@odata.id": f"{FW}b", "Version": "Not Present", "Active": False}
    found[MGR]["Links"]["SoftwareImages"].append({"@odata.id": f"{FW}b"})
    assert 'ab_bank="not reported: only empty slots"' in bmc(found), bmc(found)
    found[f"{FW}b"]["Version"] = RUNNING[:5]
    assert f'ab_bank="yes: Inactive {RUNNING[:5]}"' in bmc(found), bmc(found)


# BMC events: a writable host gets one subscription to /events/<host>, the receiver's credentials in its HttpHeaders;
# run again it is kept, --remove deletes it; a host NetBox doesn't tag writable is never touched
def test_subscribe():
    subs = "/redfish/v1/EventService/Subscriptions"
    bmc = {rollout.ROOT: {"EventService": {"@odata.id": "/redfish/v1/EventService"}},
           "/redfish/v1/EventService": {"Subscriptions": {"@odata.id": subs}}, subs: {"Members": []}}
    sent = []

    def send(t, method, uri, **kw):  # the BMC: a POST adds a subscription, a DELETE removes it
        sent.append((method, uri, kw.get("json")))
        if method == "POST":
            new = f"{subs}/{len(sent)}"
            bmc[subs]["Members"].append({"@odata.id": new})
            bmc[new] = dict(kw["json"])
            return 201, {"Location": new}, {}
        bmc[subs]["Members"] = [m for m in bmc[subs]["Members"] if m["@odata.id"] != uri]
        return 204, {}, {}
    dest, auth = "http://172.19.0.1:8088/events/127.1.0.1", {"Authorization": "Basic eA=="}
    with patch.object(rollout, "call", lambda t, u: copy.deepcopy(bmc.get(u, {"error": "404"}))), \
         patch.object(rollout, "send", send):
        assert rollout.subscription(None, dest, auth) == ("created", f"{subs}/1"), sent
        assert sent[0][2] == {"Destination": dest, "Protocol": "Redfish", "EventFormatType": "Event",
                              "Context": "127.1.0.1", "HttpHeaders": [auth]}, sent
        assert rollout.subscription(None, dest, auth) == ("kept", f"{subs}/1") and len(sent) == 1, sent
        assert rollout.subscription(None, dest, auth, remove=True) == ("removed 1 of 1", f"{subs}/1")
        assert bmc[subs]["Members"] == [], bmc[subs]
    touched = []
    servers = [{"host": "127.1.0.1", "writable": True}, {"host": "10.0.0.9", "writable": False}]
    env = {"EVENT_RECEIVER": "http://172.19.0.1:8088/", "EVENT_PASSWORD": "p"}
    with patch.dict(os.environ, env), patch.object(rollout, "load_servers", lambda: servers), \
         patch.object(rollout, "tunnel", lambda s: s), patch.object(rollout, "login", lambda t: None), \
         patch.object(rollout, "subscription", lambda t, d, h, r: touched.append((t["host"], d)) or ("created", "-")):
        rollout.subscribe(SimpleNamespace(remove=False))
    assert touched == [("127.1.0.1", dest)], touched


# The oem view's discovery: an Oem property and an Oem action of a standard resource, every property of a job only an
# Oem link leads to, a resource refused with 403; the update-related ones named so
def test_oem():
    import poller
    jobs = "/redfish/v1/Managers/1/Oem/Acme/Jobs"
    found = {poller.ROOT: {"Managers": {"@odata.id": "/redfish/v1/Managers"},
                           "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"}},
             "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
             "/redfish/v1/Managers/1": {"@odata.type": "#Manager.v1_19_0.Manager",
                                        "Oem": {"Acme": {"FanMode": "Full", "Jobs": {"@odata.id": jobs}}}},
             jobs: {"@odata.type": "#AcmeJobCollection.AcmeJobCollection", "Members": [{"@odata.id": f"{jobs}/7"}]},
             f"{jobs}/7": {"@odata.type": "#AcmeJob.v1_0_0.AcmeJob", "JobState": "Scheduled"},
             "/redfish/v1/UpdateService": {"@odata.type": "#UpdateService.v1_11_1.UpdateService", "Actions": {
                 "Oem": {"#AcmeUpdateService.Install": {"target": "/redfish/v1/UpdateService/Actions/Install"}}}},
             "/redfish/v1/Chassis/1/Drives/3": {"error": "HTTPError: 403 Client Error: Forbidden"}}
    assert poller.oem_only(found) == {jobs, f"{jobs}/7", "/redfish/v1/Chassis/1/Drives/3"}, poller.oem_only(found)
    got = {k: (v, poller.update_hint(k)) for k, v in poller.extensions(found).items()}
    assert got == {("Acme", "Manager", "FanMode"): ("Full", ""),
                   ("AcmeJob", "(Oem resource)", "JobState"): ("Scheduled", "job"),
                   ("#AcmeUpdateService.Install", "UpdateService", "Actions/target"):
                       ("/redfish/v1/UpdateService/Actions/Install", "update"),
                   ("(refused)", "/redfish/v1/Chassis/[]/Drives/[]", "-"):
                       ("HTTPError: 403 Client Error: Forbidden", "")}, got


# The network: every EthernetInterface and a NetworkAdapter's Ports, their LinkStatus and speed in Mbit/s; not the
# Manager's USB ports
def test_network():
    import poller
    port = lambda status, gbps: {"@odata.type": "#Port.v1_8_0.Port", "LinkStatus": status, "CurrentSpeedGbps": gbps,
                                 "Ethernet": {"AssociatedMACAddresses": ["6C:FE:54:9C:AD:D0"]}}
    found = {poller.ROOT: {},
             "/redfish/v1/Managers/1/EthernetInterfaces/eth0": {
                 "@odata.type": "#EthernetInterface.v1_9_0.EthernetInterface", "LinkStatus": "LinkUp",
                 "SpeedMbps": 1000, "MACAddress": "52:54:00:12:34:56", "IPv4Addresses": [{"Address": "10.0.2.15"}]},
             "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1": port("LinkDown", 25),
             "/redfish/v1/Managers/1/USBPorts/usb0": port(None, None)}
    out = [line for line in poller.exposition({"host": "h"}, found, 1.0) if line.startswith("redfish_network")]
    assert len(out) == 4 and not any("USBPorts" in line for line in out), out
    assert any('ipv4="10.0.2.15"' in line and line.startswith("redfish_network_link{") and line.endswith(" 4")
               for line in out), out
    assert any('mac="6C:FE:54:9C:AD:D0"' in line and line.startswith("redfish_network_link{") and line.endswith(" 1")
               for line in out), out
    assert sorted(line.rsplit(" ", 1)[1] for line in out if "speed" in line) == ["1000", "25000"], out


# The exporter's log passes on a fake BMC and Loki: an SEL listed oldest first, one page; a log listed newest first,
# two entries a page (iDRAC); one listed by link only (iLO 4); one whose BMC clock runs 5 hours ahead; the Journal. A
# first read sends the last days only, a later one only what is new; a clock 5 hours off is corrected, one at 1970 gets
# the read time; a log cleared between two reads gives a gap line; a Loki down sends it all again next pass, its 400
# counts as refused; a restart goes on from the state file, or without one from what Loki has: nothing goes again, not
# even what got the read time
def test_bmc_logs():
    from datetime import datetime, timedelta, timezone
    now, base = datetime.now(timezone.utc), "/redfish/v1/Managers/1/LogServices"
    ago = lambda **d: (now - timedelta(**d)).isoformat(timespec="seconds")
    entry = lambda log, n, created, severity="OK": {"@odata.id": f"{base}/{log}/Entries/{n}", "Created": created,
                                                    "Severity": severity, "Message": f"{log} {n}"}
    names = ("SEL", "Journal", "Lc", "IEL", "Ahead")
    bmc = {base: {"Members": [{"@odata.id": f"{base}/{x}"} for x in names]},
           **{f"{base}/{x}": {"Id": x, "Name": f"{x} log", "Entries": {"@odata.id": f"{base}/{x}/Entries"}}
              for x in names},
           f"{base}/IEL/Entries": {"Members": [{"@odata.id": f"{base}/IEL/Entries/{n}"} for n in (1, 2)]},
           f"{base}/IEL/Entries/1": entry("IEL", 1, ago(hours=3), "Warning"),
           # its clock: a time zone wrong by 5 hours, an entry made an hour ago by it
           f"{base}/Ahead/Entries": {"Members": [entry("Ahead", 1, ago(hours=1 - 5))]}}
    bmc[f"{base}/SEL"]["MaxNumberOfRecords"] = 4096
    bmc[f"{base}/Ahead"]["DateTime"] = ago(hours=-5)

    def logs(sel, lc):  # the SEL as one page; Lc newest first, two a page, each page linking the next
        bmc[f"{base}/SEL/Entries"] = {"Members": sel, "Members@odata.count": len(sel)}
        for i in range(0, len(lc), 2):
            more = {"Members@odata.nextLink": f"{base}/Lc/Entries?$skip={i + 2}"} if i + 2 < len(lc) else {}
            bmc[f"{base}/Lc/Entries" + (f"?$skip={i}" if i else "")] = {"Members": lc[i:i + 2], **more}
    lc = [entry("Lc", n, ago(hours=9 - n) if n > 4 else ago(days=45 - n)) for n in range(8, 0, -1)]
    fetched, pushed, held, loki = [], [], [], {"answer": (204, "")}

    class Answer:
        def __init__(self, status=204, text="", body=None):
            self.status_code, self.text, self.ok, self.body = status, text, status < 300, body

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self.body

    def post(url, json, timeout):  # Loki takes the lines it is sent, held for its queries
        if loki["answer"] is None:
            raise rollout.requests.ConnectionError("Connection refused")
        pushed.append(json)
        held.extend((s["stream"], v) for s in json["streams"] for v in s["values"])
        return Answer(*loki["answer"])

    def get(url, timeout, params):  # one host's log, as in_loki asks
        log = params["query"].split('log="')[1].split('"')[0]
        return Answer(body={"data": {"result": [{"stream": s, "values": [v]} for s, v in held
                                                if s["log"] == log and int(v[0]) >= params["start"]]}})
    state = {"logs": {}, "refused": 0}
    # a crawl that stopped before the manager (iDRAC, at its deadline): the pass reads it itself
    found = {poller.ROOT: {"Managers": {"@odata.id": "/redfish/v1/Managers"}},
             "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]}}
    bmc["/redfish/v1/Managers/1"] = {"@odata.type": "#Manager.v1_19_0.Manager", "LogServices": {"@odata.id": base}}

    def run():  # one pass: its samples, and what went to Loki as {message: (log, severity, timestamp)}
        fetched.clear(), pushed.clear()
        with (patch.object(poller, "call", lambda t, uri: fetched.append(uri) or copy.deepcopy(bmc.get(
                uri, {"error": "404 Not Found"}))),
              patch.object(poller.requests, "post", post), patch.object(poller.requests, "get", get)):
            samples = poller.read_logs({"host": "h"}, (None, "https://h", {}), found, state, "http://loki")
        return samples, {json.loads(line)["Message"]: (s["stream"]["log"].rsplit("/", 1)[1], s["stream"]["severity"],
                                                       int(t)) for b in pushed for s in b["streams"]
                         for t, line in s["values"]}
    stamp = lambda e, hours=0: poller.nanoseconds(datetime.fromisoformat(e["Created"]) - timedelta(hours=hours))
    sel = [entry("SEL", 1, ago(days=40)), entry("SEL", 2, ago(days=1), "Critical"), entry("SEL", 3, None)]
    logs(sel, lc)
    samples, sent = run()
    # the last 29 days, each at its Created on our clock; not 40 days back, nor an entry of no time. The Journal isn't
    # read, and Lc only up to its first page with nothing that recent
    assert sent == {"SEL 2": ("SEL", "Critical", stamp(sel[1])), "IEL 1": ("IEL", "Warning", stamp(
        bmc[f"{base}/IEL/Entries/1"])), "Ahead 1": ("Ahead", "OK", stamp(bmc[f"{base}/Ahead/Entries"]["Members"][0], 5)),
        **{e["Message"]: ("Lc", "OK", stamp(e)) for e in lc[:4]}}, sent
    assert f"{base}/Journal/Entries" not in fetched and f"{base}/Lc/Entries?$skip=6" not in fetched, fetched
    assert f'redfish_log_entries{{host="h",log="{base}/SEL",name="SEL log"}} 3' in samples, samples
    assert f'redfish_log_max_entries{{host="h",log="{base}/SEL",name="SEL log"}} 4096' in samples, samples
    skew = [float(s.rsplit(" ", 1)[1]) for s in samples if s.startswith('redfish_bmc_clock_skew_seconds{host="h"}')]
    assert len(skew) == 1 and abs(skew[0] - 18000) <= 1, samples  # 5 hours, to the second its clock is read at
    # what is new only: an IEL entry read once isn't read again, nor Lc past the page of the entries it knows; a clock
    # at 1970 gets the read time. The IEL entry that didn't answer is read now, still a first read: 40 days old, it's
    # history, not news
    sel += [entry("SEL", 4, ago(minutes=1), "Warning"), entry("SEL", 5, "1970-01-01T00:00:00Z")]
    bmc[f"{base}/IEL/Entries/2"] = entry("IEL", 2, ago(days=40))
    lc.insert(0, entry("Lc", 9, ago(seconds=30)))
    logs(sel, lc)
    samples, sent = run()
    assert set(sent) == {"SEL 4", "SEL 5", "Lc 9"} and sent["SEL 4"][2] == stamp(sel[3]), sent
    assert sent["SEL 5"][2] >= poller.nanoseconds(now), sent
    assert f"{base}/IEL/Entries/1" not in fetched and f"{base}/Lc/Entries?$skip=4" not in fetched, fetched
    assert f"{base}/IEL/Entries/2" in fetched and not state["logs"][f"{base}/IEL"]["first"], state["logs"]
    # the SEL cleared between two reads, its Ids taken again: a new entry, and a gap line
    logs([entry("SEL", 1, ago(seconds=20))], lc)
    samples, sent = run()
    assert sent.pop("SEL 1")[:2] == ("SEL", "OK") and len(sent) == 1, sent
    assert next(iter(sent)).startswith("Gap: none of the 5 entries") and [*sent.values()][0][:2] == ("SEL", "Warning")
    # Loki down: nothing counts as sent, the entry is pending, then sent next pass; its 400 counts as refused
    sel = [entry("SEL", 1, ago(seconds=20)), entry("SEL", 2, ago(seconds=10)), entry("SEL", 3, ago(seconds=5))]
    logs(sel, lc)
    loki["answer"] = None
    samples, sent = run()
    assert not sent and 'redfish_log_pending{host="h"} 2' in samples, samples
    loki["answer"] = (400, "entry with timestamp ... ignored, reason: 'entry too far behind',\nuser 'fake', total "
                           "ignored: 1 out of 2 for stream: {host=\"h\"}\n")
    samples, sent = run()
    assert set(sent) == {"SEL 2", "SEL 3"} and 'redfish_log_refused{host="h"} 1' in samples, (sent, samples)
    loki["answer"] = (204, "")
    logs(sel + [entry("SEL", 4, "1970-01-01T00:00:00Z")], lc)
    assert set(run()[1]) == {"SEL 4"}
    # a restart from the state file: where the reads were, Loki not asked
    with tempfile.TemporaryDirectory() as folder:
        poller.save_log_state(folder, "h", state["logs"])
        assert poller.load_log_state(folder, "h") == state["logs"], state["logs"]
        state = {"logs": poller.load_log_state(folder, "h"), "refused": 0}
    with patch.object(poller, "in_loki", lambda *a: 1 / 0):
        samples, sent = run()
    assert sent == {} and f"{base}/IEL/Entries/1" not in fetched, (sent, fetched)
    # without one: each log's first read starts from what Loki has, so nothing goes again, not even the entry that got
    # the read time, which Loki couldn't have dropped as the same line at the same time
    state = {"logs": {}, "refused": 0}
    samples, sent = run()
    assert sent == {} and f"{base}/IEL/Entries/1" not in fetched, (sent, fetched)
    # a JobService's Log (JobService v1_1_0): read as the managers' logs are
    jobs = "/redfish/v1/JobService/Log"
    found[poller.ROOT]["JobService"] = {"@odata.id": "/redfish/v1/JobService"}
    bmc["/redfish/v1/JobService"] = {"@odata.type": "#JobService.v1_1_0.JobService", "Log": {"@odata.id": jobs}}
    bmc[jobs] = {"Id": "Log", "Name": "Job log", "Entries": {"@odata.id": f"{jobs}/Entries"}}
    job = {**entry("Job", 1, ago(minutes=2), "Warning"), "@odata.id": f"{jobs}/Entries/1"}
    bmc[f"{jobs}/Entries"] = {"Members": [job]}
    assert run()[1] == {"Job 1": ("Log", "Warning", stamp(job))}

if __name__ == "__main__":
    failed = 0
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            try:
                skipped = test()  # a test that can't run returns why
                print(f"skip  {name}: {skipped}" if skipped else f"ok    {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    raise SystemExit(f"{failed} failed" if failed else 0)
