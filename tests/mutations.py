"""Proof the tests catch what they are for: break one rule of the tools at a time, the way a careless edit would, and
check the test that guards it fails. The breaks go into a copy, never into the tools: make mutations. Run it before
changing a safety rule; a rule whose line changed must be updated in BREAKS too."""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the repository: rollout.py, the tools it imports, tests/
# ponytail: hand-listed breaks of the safety rules only; a mutation tool (mutmut) once there are too many to list
BREAKS = [  # (what a careless edit does, the line as it is, the line broken, the test that must fail)
    ("downgrade let through", 'test("direction", order > 0 or allowed,', 'test("direction", True,',
     "test_preflight_direction"),
    ("versions compared as text", 'return tuple(map(int, version.split(".")))', 'return tuple(version.split("."))',
     "test_preflight_direction"),
    ("sha256 mismatch ignored", 'test("image sha256", not problems,', 'test("image sha256", True,',
     "test_preflight_image"),
    ("image never fetched from the cache", "and (error := fetch(file, path)):", "and False:", "test_image_cache"),
    ("a tampered copy kept", "    if IMAGE_CACHE:\n        path.unlink()", "    if False:\n        path.unlink()",
     "test_image_cache"),
    ("image size not checked", 'test("image size", path.stat().st_size <= limit,', 'test("image size", True,',
     "test_preflight_image"),
    ("no rollback path accepted", 'test("rollback path", result["rollback"] != "none" or policy["accept_no_rollback"],',
     'test("rollback path", True,', "test_preflight_rollback"),
    ("unhealthy BMC updated", 'test("health", h == "OK",', 'test("health", True,', "test_preflight_host"),
    ("failed task only skipped", "defer=state in RUNNING_STATES)", "defer=True)", "test_preflight_host"),
    ("Updateable false ignored", 'test("Updateable", not can.startswith("no:"),', 'test("Updateable", True,',
     "test_preflight_host"),
    ("already updated is a block", '"already on the baseline version", defer=True)',
     '"already on the baseline version")', "test_preflight_nothing_to_do"),
    ("post-check ignores the version", '{"check": "running version", "ok": running == want,',
     '{"check": "running version", "ok": True,', "test_post_check"),
    ("post-check ignores health", '{"check": "health", "ok": h == "OK" or h == was.get(u),',
     '{"check": "health", "ok": h == "OK",', "test_post_check"),
    ("post-check blames old tasks", 'if j[0] not in before["jobs"]]', "]", "test_post_check"),
    ("rollback installs the target", 'component, before["version"])\n        if not r["rollback"]',
     'component, want)\n        if not r["rollback"]', "test_run_host"),
    ("rollback before any reset", "if not passed and activated:", "if not passed:", "test_run_host"),
    ("gate halts at exactly failure_rate", 'len(failed) / len(tried) > rollout["failure_rate"]',
     'len(failed) / len(tried) >= rollout["failure_rate"]', "test_gate"),
    ("soak failure ignored", "strict or soured > 0 or", "strict or", "test_gate"),
    ("wave 1 not strict", 'range(1, rollout["strict_waves"] + 1)', 'range(1, rollout["strict_waves"])', "test_gate"),
    ("blocked hosts counted as tried", 'if s not in ("skipped", "blocked")]', 'if s != "skipped"]', "test_gate"),
    ("an A/B bank counted as a way back", '    if image and not image_problems(',
     '    if ab_bank(found, component).startswith("yes") or image and not image_problems(', "test_preflight_rollback"),
    ("no drain for a host reset", 'test("drain", policy["drain"],', 'test("drain", True,', "test_drain"),
    ("undrain failure ignored", "if not ok:  # the firmware may be fine, but the host isn't back in service",
     "if False:", "test_drain"),
    ("a gone manager passes", "for u, h in was.items() if u not in now),", "for u, h in was.items() if False),",
     "test_post_check"),
    ("canary ignores the rack limit", 'key=lambda s: (full(s), s.get("canary") is not True)',
     'key=lambda s: (False, s.get("canary") is not True)', "test_canary_racks"),
    ("changed plan inputs ignored", "if sha256_of(f) != sha]", "if False]", "test_frozen_plan"),
    ("another BMC updated", 'if planned and r["verdict"] == "go" and r.get("identity") != planned:', "if False:",
     "test_frozen_plan"),
    ("a state's series vanishes between pushes", 'in dict.fromkeys([*HOST_STATES, *w["totals"]]):', 'in w["totals"]:',
     "test_metrics_mid_run"),
    ("a host changed in the inventory runs", 'if current.get(h) != saved["servers"].get(h)]', "if False]",
     "test_frozen_plan"),
    ("writable from a config context",
     'context = {k: v for k, v in (d.get("config_context") or {}).items() if k in CONNECTION}',
     'context = dict(d.get("config_context") or {})', "test_netbox_inventory"),
    ("an untagged host run with --yes", 'if s.get("writable") is not True]', "if False]", "test_run_writable"),
    ("any secret sent as a BMC login", "elif not ACCOUNT.fullmatch(account) or not set(", "elif not set(",
     "test_netbox_inventory"),
    ("a BMC login sent over plain HTTP", 'elif context.get("scheme", "https") != "https":', "elif False:",
     "test_netbox_inventory"),
    ("a held lock let through", "if taken == set(hosts):", "if True:", "test_host_locks"),
    ("SPDM claimed for another part", "if spdm and mine & targets:", "if spdm:", "test_attestation"),
    ("a host left for a person unlocked", 'if o["state"] == "needs_attention")', "if False)", "test_host_locks"),
    ("BMC log lines lost, Loki down", 'state["logs"].setdefault(uri, known)\n                continue',
     'state["logs"].setdefault(uri, known)', "test_bmc_logs"),
    ("a cleared log's entries missed", "if u not in seen or seen[u] != read[u]:", "if u not in seen:",
     "test_bmc_logs"),
    ("a log gap not told", 'if complete and known["seen"] and read and not any(', "if False and not any(",
     "test_bmc_logs"),
    ("a wrong BMC clock's time sent", "ok = t and not late and t <= now + timedelta(minutes=5)", "ok = t",
     "test_bmc_logs"),
    ("a BMC clock hours off kept", "if clock and abs(skews[-1]) >= LOG_SKEW else", "if False else", "test_bmc_logs"),
    ("a restart sends again what Loki has", "seen, newest = in_loki(loki, host, uri, history)",
     "seen, newest = {}, None", "test_bmc_logs"),
    ("a cut-short first read's history sent", '"first": bool(known.get("first")) and not complete}', '"first": False}',
     "test_bmc_logs"),
    ("the log state file not read back", '    return {uri: {**k, "newest": k["newest"] and datetime.fromisoformat(',
     '    return {} and {uri: {**k, "newest": k["newest"] and datetime.fromisoformat(', "test_bmc_logs"),
    ("the BMC's journal sent", 'if service.get("Id") in LOG_SKIP or not entries:', "if not entries:",
     "test_bmc_logs"),
    ("the JobService's log not read", '| ({link(get(jobs), "Log")} - {None} if jobs else set())', "",
     "test_bmc_logs"),
]

if __name__ == "__main__":
    tools = {f.name: f.read_text() for f in ROOT.glob("*.py")}  # rollout.py and the tools it imports
    missed = []
    with tempfile.TemporaryDirectory() as copy:
        for f in ROOT.glob("*.py"):
            shutil.copy(f, copy)
        shutil.copytree(ROOT / "tests", Path(copy) / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        (Path(copy) / "control" / "ingest").mkdir(parents=True)  # the ingest's test runs its scripts
        for script in ("verify-fw-package.sh", "promote-store-fw-package.sh"):
            shutil.copy2(ROOT / "control" / "ingest" / script, Path(copy) / "control" / "ingest")
        for what, line, broken, test in BREAKS:
            where = [name for name, text in tools.items() if line in text]
            if len(where) != 1 or tools[where[0]].count(line) != 1:
                sys.exit(f"the line {what!r} breaks is in {', '.join(where) or 'no tool'}, not once in one tool. "
                         "Update it in BREAKS.")
            (Path(copy) / where[0]).write_text(tools[where[0]].replace(line, broken))
            out = subprocess.run([sys.executable, "tests/test_rollout.py"], cwd=copy, capture_output=True,
                                 text=True).stdout
            (Path(copy) / where[0]).write_text(tools[where[0]])  # whole again for the next break
            failing = re.findall(r"^FAIL  (\w+)", out, re.M)
            print(f"{'caught' if test in failing else 'MISSED'}  {what:32} {', '.join(failing) or 'no test failed'}")
            missed += [what] if test not in failing else []
    print(f"{len(BREAKS) - len(missed)} of {len(BREAKS)} breaks caught")
    sys.exit(1 if missed else 0)
