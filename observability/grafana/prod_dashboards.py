"""Prod's dashboards, made from lab's: dashboards/lab/*.json into dashboards/prod/, the same panels fixed to site prod,
and on the pipeline a note that production is read-only. Not the update details, which document the lab's BMC. Edit
the lab dashboards only; make monitor-up runs this."""
import json
import re
from pathlib import Path

DASHBOARDS = Path(__file__).parent / "dashboards"
NOTE = {"type": "text", "title": "", "id": 99, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3}, "transparent": True,
        "options": {"mode": "markdown", "content": (
            "**Production is read-only.** `rollout.py run --yes` refuses hosts not marked `writable: true`, and none in "
            "`prod/inventory.yaml` is, so no pipeline pushes here. `make plan SITE=prod` shows what a rollout would do.")}}


# A dashboard with lab's site, title, folder link, ids and links between dashboards made prod's
def prod(x):
    if isinstance(x, dict):
        return {k: prod(v) for k, v in x.items()}
    if isinstance(x, list):
        return [prod(v) for v in x]
    if isinstance(x, str):
        return re.sub(r"\blab-(?=fleet|host|pipeline)", "prod-", re.sub(r"^Lab\b", "Prod", re.sub(r"^lab$", "prod", x)))
    return x


if __name__ == "__main__":
    (DASHBOARDS / "prod").mkdir(exist_ok=True)
    for lab in sorted((DASHBOARDS / "lab").glob("*.json")):
        if lab.stem == "details":  # what a lab BMC was checked to do, not a fleet's
            continue
        doc = prod(json.loads(lab.read_text()))
        if lab.stem == "pipeline":  # nothing pushes from prod: the note on top, every panel below it
            for panel in doc["panels"]:
                panel["gridPos"]["y"] += NOTE["gridPos"]["h"]
            doc["panels"].insert(0, NOTE)
        text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
        left = re.findall(r'"[Ll]ab"|"Lab |lab-(?:fleet|host|pipeline)', text)
        if left:
            raise SystemExit(f"{lab.name}: lab left in the prod dashboard, {sorted(set(left))}: extend prod()")
        (DASHBOARDS / "prod" / lab.name).write_text(text)
