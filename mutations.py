"""Proof the tests catch what they are for: break one rule of rollout.py at a time, the way a careless edit would, and
check the test that guards it fails. The breaks go into a copy, never into rollout.py: make mutations. Run it before
changing a safety rule; a rule whose line changed must be updated in BREAKS too."""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
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
    ("unhealthy BMC updated", 'test("health", health == "OK",', 'test("health", True,', "test_preflight_host"),
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
    ("gate halts at exactly halt_at", 'len(failed) / len(tried) > rollout["halt_at"]',
     'len(failed) / len(tried) >= rollout["halt_at"]', "test_gate"),
    ("soak failure ignored", "strict or soured > 0 or", "strict or", "test_gate"),
    ("wave 1 not strict", 'range(1, rollout["strict_waves"] + 1)', 'range(1, rollout["strict_waves"])', "test_gate"),
    ("blocked hosts counted as tried", 'if s not in ("skipped", "blocked")]', 'if s != "skipped"]', "test_gate"),
    ("an A/B bank counted as a way back", '    if image and not image_problems(',
     '    if ab_bank(found, component).startswith("yes") or image and not image_problems(', "test_preflight_rollback"),
    ("no drain for a host reset", 'test("drain", policy["drain"],', 'test("drain", True,', "test_drain"),
    ("undrain failure ignored", "if not ok:  # the firmware may be fine, but the host isn't back in service",
     "if False:", "test_drain"),
    ("a gone manager passes", "                if u not in now),", "                if False),", "test_post_check"),
    ("canary ignores the rack limit", 'key=lambda s: (full(s), s.get("canary") is not True)',
     'key=lambda s: (False, s.get("canary") is not True)', "test_canary_racks"),
    ("changed plan inputs ignored", "if sha256_of(f) != sha]", "if False]", "test_frozen_plan"),
    ("another BMC updated", 'if planned and r["verdict"] == "go" and r.get("identity") != planned:', "if False:",
     "test_frozen_plan"),
]

if __name__ == "__main__":
    original = (HERE / "rollout.py").read_text()
    missed = []
    with tempfile.TemporaryDirectory() as copy:
        for f in HERE.glob("*.py"):
            shutil.copy(f, copy)
        shutil.copytree(HERE / "fixtures", Path(copy) / "fixtures")
        (Path(copy) / "lab").mkdir()
        shutil.copy2(HERE / "lab" / "promote-fw-images.sh", Path(copy) / "lab")  # the ingest gate's test runs it
        for what, line, broken, test in BREAKS:
            if original.count(line) != 1:
                sys.exit(f"rollout.py changed: the line {what!r} breaks is there {original.count(line)} times, not "
                         "once. Update it in BREAKS.")
            (Path(copy) / "rollout.py").write_text(original.replace(line, broken))
            out = subprocess.run([sys.executable, "test_rollout.py"], cwd=copy, capture_output=True, text=True).stdout
            failing = re.findall(r"^FAIL  (\w+)", out, re.M)
            print(f"{'caught' if test in failing else 'MISSED'}  {what:32} {', '.join(failing) or 'no test failed'}")
            missed += [what] if test not in failing else []
    print(f"{len(BREAKS) - len(missed)} of {len(BREAKS)} breaks caught")
    sys.exit(1 if missed else 0)
