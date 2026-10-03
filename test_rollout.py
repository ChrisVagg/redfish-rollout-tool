"""Checks of the rollout's decisions, the parts that decide how far a bad image can reach: python test_rollout.py
(make test). No BMC is needed: pre-flight and the post-check read the lab BMC as a collect saved it, and the update
itself is replaced, so what is tested is what the rollout decides, from real Redfish data."""
import copy
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

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


# ---- Pre-flight, the post-check, the rollback and the gate, on the lab BMC ----

# The lab BMC as a collect read it: OpenBMC's GB200 NVL build 1754, healthy, no task open
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "openbmc-gb200.json").read_text())["resources"]
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


# No way back, one bank and no image of the running version, blocks unless --accept-no-rollback
def test_preflight_rollback():
    no_way_back = catalog(**{TARGET: image("1755.tar")})
    verdict, reason = preflight(images=no_way_back)
    assert verdict == "block" and "--accept-no-rollback to update anyway" in reason, reason
    assert preflight(images=no_way_back, accept_no_rollback=True) == ("go", "")


# The BMC itself: unreachable, a component Redfish can't update, not healthy (heal.sh's degraded boot), or a task
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
    # A task that failed since the update fails it; one already open before the update doesn't
    found = running(lab(), TARGET)
    found[TASK] = task("Exception", "Critical")
    assert rollout.after_checks(found, BMC, TARGET, before)[0] is False
    assert rollout.after_checks(found, BMC, TARGET, rollout.before_state(found, RUNNING))[0] is True


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


# The gate after each wave, the lab's policy: the canary and wave 1 strict, later waves halt over 10% failed.
# states: every host tried so far, every wave up to this one
def test_gate():
    policy = {"strict_waves": 1, "halt_at": 0.1}
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
        {"at": at("00:00"), "host": "-", "step": "run", "state": "start", "waves": {"wave 1": ["h1"]}, "halt_at": 0.1},
        {"at": at("00:01"), "host": "h1", "step": "pre-flight", "state": "go", "running": "1.0", "target": "2.0"},
        {"at": at("00:02"), "host": "h1", "step": "update", "state": "start", "method": "push"},
        {"at": at("00:09"), "host": "h1", "step": "update", "state": "Exception", "task": {
            "uri": "/redfish/v1/TaskService/Tasks/0", "state": "Exception", "messages": [
                {"severity": "Critical", "message_id": "TaskEvent.1.0.TaskAborted", "message": "aborted"}]}}]
    doc = rollout.pipeline_report(None, events, "run", [])
    assert doc["result"] == "running" and doc["waves"][0]["hosts"][0]["state"] == "running"
    text = rollout.metrics(doc)
    assert 'rollout_hosts{wave="wave 1",state="running"} 1' in text and "rollout_running 1" in text


# The exporter's samples are one series each: two sensors of one name (iLO 4's power supplies) stay two series.
# Thresholds take DMTF's Sensor names and their reading's unit, a Fan's too (its schema gives them none)
def test_exposition():
    import poller
    poller.schema_file = lambda name: {}  # offline: the unit comes from ReadingUnits
    # lambda n: the n-th sensor, every one named PSU
    sensor = lambda n: {"@odata.type": "#Sensor.v1_2_0.Sensor", "Name": "PSU", "Reading": 30 + n, "ReadingUnits": "Cel",
                        "Thresholds": {"UpperCritical": {"Reading": 90}}, "Status": {"Health": "Warning"}}
    fan = {"Name": "Fan1", "Reading": 5000, "ReadingUnits": "RPM", "LowerThresholdNonCritical": 600,
           "UpperThresholdCritical": 0}  # an upper 0: no threshold set (iLO 4)
    found = {poller.ROOT: {}, **{f"/redfish/v1/Chassis/1/Sensors/{n}": sensor(n) for n in (1, 2)},
             "/redfish/v1/Chassis/1/Thermal": {"@odata.type": "#Thermal.v1_7_0.Thermal", "Fans": [fan]}}
    out = poller.exposition({"host": "h"}, found, 1.0)
    series = [line.rsplit(" ", 1)[0] for line in out]
    assert len(series) == len(set(series)) and sum(s.startswith("redfish_reading{") for s in series) == 3, series
    thresholds = sorted(line for line in out if line.startswith("redfish_reading_threshold"))
    assert len(thresholds) == 3 and all('threshold="UpperCritical"} 90' in t for t in thresholds[1:]), thresholds
    assert 'sensor="Fan1",property="Reading",unit="RPM",threshold="LowerCaution"} 600' in thresholds[0], thresholds
    assert sum(line.startswith("redfish_health{") and 'health="Warning"' in line for line in out) == 2
    crossed = sorted(line.rsplit(" ", 1)[1] for line in out if line.startswith("redfish_reading_crossed"))
    assert crossed == ["0", "0", "0"], crossed  # the sensors under their 90, the fan over its 600
    fan["Reading"] = 500  # under its lower caution
    assert any(line.startswith("redfish_reading_crossed{") and 'sensor="Fan1"' in line and line.endswith(" 1")
               for line in poller.exposition({"host": "h"}, found, 1.0))


if __name__ == "__main__":
    failed = 0
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            try:
                test()
                print(f"ok    {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    raise SystemExit(f"{failed} failed" if failed else 0)
