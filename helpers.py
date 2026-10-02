"""Helpers of the Redfish poller: reading Redfish documents, finding resources in a crawl, tables, snapshot files
and output files."""
import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.table import Column, Table
from rich.text import Text

from constants import ACTIONS, DISCRETE, OEM, PLACEHOLDERS, PREVIOUS, REASON, ROOT, SITE, SNAPSHOTS, STYLE, TYPE

log = logging.getLogger("redfish")


# ---- Errors, URIs and types ----

# The reason inside a requests error message, without host, URL or object reprs
def reason(message) -> str:
    return next(iter(REASON.findall(message)), message).strip()


# A URI without its #fragment and trailing slash, so each resource has one key
def norm(uri) -> str:
    return uri.split("#")[0].rstrip("/")


# A resource URI plus a JSON pointer when keys lead into it: '/redfish/v1/Chassis/1/Thermal#/Fans/0'
def pointer(uri, keys) -> str:
    return "#/".join(filter(None, (uri, "/".join(keys))))


# The type name and schema version of a document: '#Chassis.v1_14_0.Chassis' -> ('Chassis', 'v1_14_0')
def rtype(doc) -> tuple[str, str]:
    m = TYPE.match(doc.get("@odata.type") or "")
    return m[1] or "(untyped)", m[2] or ""


# ---- Reading documents ----

# Every leaf property of a document as {path: value}, e.g. {"Status/Health": "OK", "Temperatures/0/Name": ...}
def properties(node, path="") -> dict[str, Any]:
    """Objects and arrays are walked into; values, and empty {} or [], are the leaves."""
    if isinstance(node, dict) and node:
        children = node.items()
    elif isinstance(node, list) and node:
        children = enumerate(node)
    else:
        return {path[1:]: node}
    return {p: v for key, child in children for p, v in properties(child, f"{path}/{key}").items()}


# The value at a path of keys and array indexes inside a document; None when the path leads nowhere
def at(doc, keys) -> Any:
    for key in keys:
        if isinstance(doc, dict):
            doc = doc.get(key)
        elif isinstance(doc, list):
            doc = doc[int(key)]
        else:
            return None
    return doc


# The URI of a link object {"@odata.id": ...}; None when it isn't one
def uri_of(x) -> str | None:
    return norm(obj(x).get("@odata.id") or "") or None


# The URI a document links to under key, e.g. link(root, "Systems") -> "/redfish/v1/Systems"; None without one
def link(doc, key) -> str | None:
    return uri_of(doc.get(key))


# x when it is a JSON object, else {}: BMCs send null where an object belongs
def obj(x) -> dict:
    return x if isinstance(x, dict) else {}


# The display name of an object: Name, else MemberId, else Id
def named(x) -> str | None:
    return obj(x).get("Name") or obj(x).get("MemberId") or obj(x).get("Id")


# A value as table text: strings as they are, None as "-", anything else as JSON
def text(v) -> str:
    if v is None:
        return "-"
    return v if isinstance(v, str) else json.dumps(v)


# False for what firmware reports instead of a value ("To be filled by O.E.M.", "N/A", ...)
def real(v) -> bool:
    return text(v).strip().casefold() not in PLACEHOLDERS


# The first path of 'A|B' holding a real value, else A: for BMCs that file a value elsewhere
def choose(flat, column) -> str:
    paths = column.split("|")
    # lambda p: a property path -> True when the document has a real value there
    return next(filter(lambda p: real(flat.get(p)), paths), paths[0])


# (property, value) for each field path the resource has, in the order asked
def pick(doc, fields) -> list[tuple[str, str]]:
    flat = properties(doc)
    return [(f, text(flat[f])) for f in filter(flat.__contains__, fields)]


# (property, value) for every standard leaf property, or with oem=True every vendor Oem one
def details(doc, oem=False) -> list[tuple[str, str]]:
    """Annotations (@odata.id, @odata.type, ...) and Actions (operations, not properties) are left out."""
    # lambda pv: a (path, value) pair -> True when the path is no annotation and no action, and is under Oem
    # exactly when oem=True
    keep = lambda pv: "@" not in pv[0] and not ACTIONS.search(pv[0]) and bool(OEM.search(pv[0])) == oem
    return [(p, text(v)) for p, v in filter(keep, properties(doc).items())]


# Health, HealthRollup and State of an object's Status exactly as the BMC reports them
def discrete(d) -> tuple[str, str, str]:
    """"-" when a value isn't reported (iLO sends "Status": null on unlinked NICs)."""
    status = obj(d.get("Status"))
    return tuple(text(status.get(k)) for k in DISCRETE)


# ---- Finding resources in a crawl ({uri: document}) ----

# (uri, document) for every resource of one type, sorted by URI
def of(found, name) -> list[tuple[str, dict]]:
    # lambda ud: a (uri, document) pair -> True when the document's type is `name`
    return sorted(filter(lambda ud: rtype(ud[1])[0] == name, found.items()))


# (uri, item) for a resource type, or for the items of an inline array such as "Power/PowerSupplies"
def items(found, source) -> list[tuple[str, Any]]:
    """An array item's uri points into the array: '/redfish/v1/Chassis/1/Power#/PowerSupplies/0'."""
    name, _, key = source.partition("/")
    if not key:
        return of(found, name)
    return [(f"{u}#/{key}/{i}", x) for u, d in of(found, name) for i, x in enumerate(d.get(key) or [])]


# The first resource of a type, {} when there is none
def first_of(found, name) -> dict:
    return next(iter(of(found, name)), ("", {}))[1]


# The first real value of a property in the systems, then the chassis
def first_real(found, prop) -> str:
    """Systems carry SMBIOS data, empty until the host has booted once; chassis carry the FRU data."""
    candidates = [d for n in ("ComputerSystem", "Chassis") for _, d in of(found, n)]
    return text(next(filter(real, (d.get(prop) for d in candidates)), None))


# (manufacturer, model) of a host
def identity(found) -> tuple[str, str]:
    return first_real(found, "Manufacturer"), first_real(found, "Model")


# The BMC's own verdict for the whole system: HealthRollup of the first ComputerSystem, as reported
def rollup(found) -> str:
    return text(obj(first_of(found, "ComputerSystem").get("Status")).get("HealthRollup"))


# (host, vendor, project) of a server from the inventory, or of its snapshot
def label(server) -> tuple[str, str, str]:
    return server["host"], str(server.get("vendor", "-")), str(server.get("project", "-"))


# Connected; Failed: the service root didn't answer; Stopped: the BMC stopped answering during the crawl
def connection(snap) -> str:
    if snap.get("stopped"):
        return "Stopped"
    return "Failed" if "error" in snap["resources"].get(ROOT, {}) else "Connected"


# One Prometheus sample in the text format, label values escaped: metric{label="value",...} value
def sample(metric, value, **labels) -> str:
    # lambda v: a label value -> escaped for the text format
    esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return metric + ("{" + ",".join(f'{k}="{esc(v)}"' for k, v in labels.items()) + "}" if labels else "") + f" {value}"


# ---- Tables ----

# The style of a cell: by value (OK green, Critical red, ...), and a true *Enabled property green
def style(key, value) -> str:
    if key.endswith("Enabled") and value == "true":  # ServiceEnabled, ProtocolEnabled, ...
        return "green"
    return STYLE.get(value, "")


# A Rich table of rows; without rows it returns the empty rows, so an empty section renders nothing
def block(title, headers, rows, key=lambda header, row: header, **table) -> Table | list:
    """Cells are Text, so BMC strings are never markup. Cells are styled by value, and by key(header, row) for
    *Enabled properties; the default key lambda names the cell by its column header. Nothing is cut: a column is at
    least as wide as its name, and a value too long for the terminal folds onto the next line instead of ending
    in '…'."""
    t = Table(*(Column(h, overflow="fold", min_width=len(h)) for h in headers),
              **{"title": title, "title_justify": "left", "title_style": "bold", "expand": True, **table})
    for row in rows:
        t.add_row(*(Text(c, style=style(key(h, row), c)) for h, c in zip(headers, row)))
    return rows and t


# How often each value occurs, most common first: "12 OK · 1 Warning"
def tally(values) -> str:
    return " · ".join(f"{n} {v}" for v, n in Counter(values).most_common())


# ---- Snapshots: collect writes one per host; the views read them ----

# The snapshot file of a host: <site>/snapshots/<host>.json
def snapshot_path(host) -> Path:
    return SNAPSHOTS / (re.sub(r"[^\w.-]", "_", host) + ".json")


# The crawl of a host before the latest one, {} when there is none
def previous(host) -> dict:
    try:
        return json.loads((PREVIOUS / snapshot_path(host).name).read_text())
    except FileNotFoundError:
        return {}


# The latest snapshot of every host; exits when there are none yet
def load() -> list[dict]:
    snaps = [json.loads(p.read_text()) for p in sorted(SNAPSHOTS.glob("*.json"))]
    if not snaps:
        sys.exit(f"no snapshots of {SITE.name} yet: run make {SITE.name}-collect first")
    return snaps


# Hosts crawled completely; a stopped crawl is partial and would show missing parts as drift
def reachable() -> list[dict]:
    # lambda s: a snapshot -> True when its host answered for the whole crawl
    return list(filter(lambda s: connection(s) == "Connected", load()))


# fn(snapshot) for every host; a host whose data breaks fn is logged and left out
def per_host(fn, snaps) -> list:
    return [r for snap in snaps for r in attempt(fn, snap)]


# [fn(snap)], or [] with the traceback logged: BMC payloads are untrusted
def attempt(fn, snap) -> list:
    try:
        return [fn(snap)]
    except Exception:
        log.exception("%s: left out", snap["host"])
        return []


# ---- Output files: every command returns what it shows; main prints it and export writes the files ----

# What was printed, in order, with Groups (a host's detail report) opened up into their tables and lines
def flatten(renderables) -> list:
    return [x for r in renderables for x in (flatten(r.renderables) if isinstance(r, Group) else [r])]


# A Rich table as data: title, caption, column names, and rows keyed by column name, cells exactly as shown
def table_data(t) -> dict:
    columns = [str(c.header).replace("\n", " ") for c in t.columns]
    cells = [[getattr(x, "plain", str(x)) for x in c.cells] for c in t.columns]
    return {"title": str(t.title or ""), "caption": str(t.caption or ""), "columns": columns,
            "rows": [dict(zip(columns, row)) for row in zip(*cells)]}


# One CSV file per table, numbered in print order: DIR/01_Health_by_host.csv, DIR/02_Not_OK.csv, ...
def write_csv(directory, tables) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for n, table in enumerate(tables, 1):
        name = re.sub(r"[^\w.-]+", "_", table["title"]).strip("_")
        with open(directory / f"{n:02d}_{name}.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=table["columns"])
            writer.writeheader()
            writer.writerows(table["rows"])


# --json FILE: every table and note line of the output; --csv DIR: one CSV per table
def export(renderables, args) -> None:
    shown = flatten(renderables)
    tables = [table_data(t) for t in shown if isinstance(t, Table)]
    notes = [t.plain for t in shown if isinstance(t, Text)]
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"command": args.command, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "tables": tables, "notes": notes}, indent=2, ensure_ascii=False))
    if args.csv:
        write_csv(Path(args.csv), tables)


# The options every command takes: files written besides the terminal output
def output_options() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", metavar="FILE", help="also write every table as JSON")
    output.add_argument("--csv", metavar="DIR", help="also write one CSV file per table into DIR")
    output.add_argument("--html", metavar="FILE", help="also write the output as HTML, colors included")
    output.add_argument("--svg", metavar="FILE", help="also write the output as an SVG terminal screenshot")
    return output


# Print what a command returns, then write the files asked for with --json, --csv, --html and --svg
def show(renderables, args) -> None:
    console = Console(record=True)
    for r in renderables:  # one print each: printed together, consecutive Texts would join on one line
        console.print(r)
    export(renderables, args)
    if args.html:
        console.save_html(args.html, clear=False)
    if args.svg:
        console.save_svg(args.svg, title=" ".join(Path(a).name for a in sys.argv[:2]))
