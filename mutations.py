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
]

if __name__ == "__main__":
    original = (HERE / "rollout.py").read_text()
    missed = []
    with tempfile.TemporaryDirectory() as copy:
        for f in HERE.glob("*.py"):
            shutil.copy(f, copy)
        shutil.copytree(HERE / "fixtures", Path(copy) / "fixtures")
        (Path(copy) / "lab").mkdir()
        for f in ("promote-fw-images.sh", "openbmc-dev.pub"):  # the ingest gate's test runs it
            shutil.copy2(HERE / "lab" / f, Path(copy) / "lab")
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
