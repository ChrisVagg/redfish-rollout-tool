"""Prod's dashboards, made from lab's: dashboards/lab/*.json into dashboards/prod/, the same panels fixed to site prod,
and on the pipeline a note that production is read-only. Not the update details, which document the lab's BMC. Edit
the lab dashboards only; make control-up runs this."""
import json
import re
from pathlib import Path

DASHBOARDS = Path(__file__).parent / "dashboards"
NOTE = {"type": "text", "title": "", "id": 99, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3}, "transparent": True,
        "options": {"mode": "markdown", "content": (
            "**Production is read-only.** `rollout.py run --yes` refuses hosts not tagged `writable` in NetBox, and no "
            "host of site `prod` is, so no pipeline pushes here. `make plan SITE=prod` shows what a rollout would "
            "do.")}}
# Grafana's Group to nested tables: a row per project, its hosts in a table under it. A field aggregated into the
# project's row leaves the hosts' table, so the row's are copies of the Redfish (I) and health (J) queries: how many
# hosts, the worst Redfish (0 down) and the worst health of them, so a collapsed project still shows a host down
BY_PROJECT = [
    {"id": "groupToNestedTable", "options": {"showSubframeHeaders": True, "fields": {
        "Project": {"operation": "groupby", "aggregations": []},
        "Value #I": {"operation": "aggregate", "aggregations": ["count", "min"]},
        "Value #J": {"operation": "aggregate", "aggregations": ["max"]}}}},
    {"id": "organize", "options": {"renameByName": {
        "Value #I (count)": "Hosts", "Value #I (min)": "Redfish", "Value #J (max)": "Worst health"}}}]


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
        if lab.stem == "fleet":  # prod's hosts belong to projects: the Hosts table grouped by project
            hosts = next(p for p in doc["panels"] if p["title"] == "Hosts")
            redfish, health = hosts["targets"][:2]
            hosts["targets"] += [{**redfish, "refId": "I"}, {**health, "refId": "J"}]
            hosts["transformations"] += BY_PROJECT
            hosts["description"] += (" Grouped by project: a row each, with its hosts' count and the worst Redfish and "
                                     "health among them; open it for its hosts.")
            for panel in doc["panels"]:  # taller, room for an open project under the projects: the rest below it
                if panel["gridPos"]["y"] > hosts["gridPos"]["y"]:
                    panel["gridPos"]["y"] += 6
            hosts["gridPos"]["h"] += 6
            overrides = hosts["fieldConfig"]["overrides"]  # the hosts' table takes only the overrides scoped nested
            overrides += [{**o, "matcher": {**o["matcher"], "scope": "nested"}} for o in overrides]
            hosts["options"]["sortBy"] = [{"displayName": "Project", "desc": False}]
        text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
        left = re.findall(r'"[Ll]ab"|"Lab |lab-(?:fleet|host|pipeline)', text)
        if left:
            raise SystemExit(f"{lab.name}: lab left in the prod dashboard, {sorted(set(left))}: extend prod()")
        (DASHBOARDS / "prod" / lab.name).write_text(text)
