#!/usr/bin/env python3
"""Redfish fleet report: one collector, several views.

  collect [INVENTORY]       Crawl every BMC once, in parallel: one persistent HTTPS connection (tunnel) per BMC,
                            the Redfish tree discovered recursively from /redfish/v1 (every link except logs,
                            sessions, accounts and the like). Saves one snapshot per host in <site>/snapshots and
                            prints the connection summary. The views below read the snapshots, not the BMCs.
  health                    What is broken right now: per host the component health counts, then every component
                            that is not OK, every job or task that failed or hasn't finished, and every failed
                            request, worst first.
  firmware                  Firmware per hardware model: hosts x components. Checked against the baseline when it
                            lists the model (green: the approved version, red: not), else a version that differs from
                            the most common one highlighted. Under each version, the component's other images as the
                            BMC reports them (Staged, Armed, Inactive) and its LowestSupportedVersion.
  inventory                 What we have: one row per host with identity, CPUs, GPUs, memory, drives, NICs, PSUs.
  telemetry                 Every sensor of every host, all kinds, with and without thresholds; before them per
                            host the hottest temperature against its critical threshold and the highest power.
  capabilities              What each hardware model can do: Redfish version, services, actions, firmware update
                            methods, telemetry support.
  diff                      What changed on each host between the previous collect and the latest one.
  detail HOST               Everything about one host: inventory, services, actions, health and telemetry. A
                            reading's unit comes from the BMC's MetricDefinitions, else the DMTF JSON Schema of the
                            resource (downloaded once into ./schemas), else the resource itself (ReadingUnits).

The site is the fleet the poller reads: SITE=prod (the default) or SITE=lab, a folder with the inventory
(INVENTORY is <site>/inventory.yaml by default), the baseline and the snapshots."""
import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import urllib3
import yaml
from requests.adapters import HTTPAdapter
from rich.console import Group
from rich.rule import Rule
from rich.table import Column, Table
from rich.text import Text
from urllib3.util.retry import Retry

from constants import (ACTION, ALLOWED, BASELINE, CAPABILITIES_ROW, CHASSIS, COMPLIANCE_ROW, CRITICAL, DMTF, FIELDS,
                       FW_PARTS, HEADERS, INVENTORY_FILE, INVENTORY_ROW, JOBS_ROW, JOB_TYPES, KIND, LIMIT, LINKS,
                       MANAGER, OEM_PATH, PREVIOUS, PROPS, READINGS, ROOT, SCHEMAS, SENSORS, SERVICE, SEVERITY, SILENT,
                       SKIP, STATUS_LEAF, SUMMARY, SYSTEM, TELEMETRY_ROW, TEMPLATES, VOLATILE, WILDCARD)
from helpers import (at, block, choose, connection, details, discrete, first_of, first_real, identity, items, label,
                     load, named, norm, obj, of, output_options, per_host, pick, pointer, previous, properties,
                     reachable, real, reason, rollup, rtype, show, snapshot_path, tally, text)

log = logging.getLogger("redfish")
_schema_lock = threading.Lock()  # one schema file read or downloaded at a time across the host threads


# ---- Connection: one persistent HTTPS connection per BMC; every call goes over it ----

# A keep-alive HTTPS connection to one BMC with Basic auth, and the limits of its crawl
def tunnel(server) -> tuple[requests.Session, str, dict]:
    """One socket: the BMCs speak HTTP/1.1 only (no HTTP/2 multiplexing), so calls go one after another over it.
    Limits, from the inventory [default]:
      timeout       [5, 30]  seconds per request: connect, read
      retries       [2]      per request, back-off 1s, 2s, 4s...: dropped connections, timeouts, 429/502/503/504
                             (a Retry-After header is honoured)
      max_failures  [3]      requests in a row without any answer before the host is given up
      deadline      [600]    seconds for the whole crawl of one host
    Returns (session, base URL, limits); limits also counts the failures in a row and holds the deadline time."""
    session = requests.Session()
    try:
        session.auth = (os.environ[server["username_env"]], os.environ[server["password_env"]])
    except KeyError as e:
        sys.exit(f"{server['host']}: env var {e} not set: run it through make (make help), or export it first")
    session.verify = server["verify"]
    session.headers["Accept"] = "application/json"
    retry = Retry(total=server.get("retries", 2), backoff_factor=1, status_forcelist=[429, 502, 503, 504])
    session.mount(server["scheme"] + "://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=retry))
    limits = {"timeout": tuple(server.get("timeout", (5, 30))), "max_failures": server.get("max_failures", 3),
              "deadline": server.get("deadline", 600), "until": float("inf"), "failures": 0}
    return session, f"{server['scheme']}://{server['host']}", limits


# GET one resource over the BMC's connection; a failed GET comes back as {"error": reason} for the report
def call(tunnel, uri) -> dict:
    """Raises ConnectionAbortedError, which stops the crawl of this host, past the host's deadline, when this is the
    max_failures-th request in a row without an answer, or on a 401: every further request would be another failed
    login, and BMCs lock the account after a few."""
    session, base, limits = tunnel
    if time.monotonic() > limits["until"]:
        raise ConnectionAbortedError(f"stopped at the {limits['deadline']}s deadline")
    try:
        r = session.get(base + uri, timeout=limits["timeout"])
        limits["failures"] = 0  # answer with error status
        if r.status_code == 401:
            raise ConnectionAbortedError("Incorrect credentials.")
        r.raise_for_status()
        return r.json()
    except SILENT as e:
        limits["failures"] += 1
        error = f"{type(e).__name__}: {reason(str(e))}"
        if limits["failures"] >= limits["max_failures"]:
            raise ConnectionAbortedError(f"not responding, {limits['failures']} requests in a row: {error}")
        return {"error": error}
    except ValueError:
        content = r.headers.get("Content-Type", "no content type")
        return {"error": f"Answer not from redfish service: {content} from {r.url}"}
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {reason(str(e))}"}


# Swap Basic auth for a Redfish session token; returns the session URI to log out, or None
def login(tunnel) -> str | None:
    """iDRAC re-checks the password on every Basic-auth request (~4s each), a token costs one login. On None,
    Basic auth stays."""
    session, base, limits = tunnel
    uri = obj(obj(call(tunnel, ROOT).get("Links")).get("Sessions")).get("@odata.id")  # None: unreachable, discover says
    if not uri:
        return None
    user, password = session.auth
    try:
        r = session.post(base + uri, json={"UserName": user, "Password": password}, timeout=limits["timeout"])
    except requests.RequestException as e:
        log.warning("%s: login: %s, staying on Basic auth", base, reason(str(e)))
        return None
    token = r.headers.get("X-Auth-Token")  # iDRAC sends one even with an error status
    if not token:
        return None
    session.auth, session.headers["X-Auth-Token"] = None, token
    return r.headers.get("Location")


# Close the session: BMCs allow only a handful, a leaked one holds a slot until it times out
def logout(tunnel, location) -> None:
    session, base, limits = tunnel
    try:
        session.delete(urljoin(base, location), timeout=limits["timeout"])
    except requests.RequestException as e:
        log.warning("%s: logout: %s", base, reason(str(e)))


# ---- Discovery: recursive from the service root, each resource once ----

# Every resource a document links to, except SKIP branches and links inside TEMPLATES annotations
def links(doc) -> list[str]:
    # lambda kv: a (key, value) pair of the document -> True when the key isn't a template annotation
    resource = dict(filter(lambda kv: kv[0] not in TEMPLATES, doc.items()))
    uris = map(norm, LINKS.findall(json.dumps(resource)))
    # lambda u: a linked URI -> True when it is a Redfish resource outside the SKIP branches
    return sorted(set(filter(lambda u: u.startswith(ROOT) and not SKIP.search(u), uris)))


# Fetch uri, then every resource it links to that isn't in found yet, recursively: {uri: document}
def discover(tunnel, uri, found) -> dict:
    # ponytail: one stack frame per link hop; iterate with an explicit stack if a BMC ever nests ~900 deep
    found[uri] = call(tunnel, uri)
    # lambda link: a linked URI -> True when it hasn't been fetched yet
    for link in filter(lambda link: link not in found, links(found[uri])):
        discover(tunnel, link, found)
    return found


# ---- Schema: the DMTF JSON Schema of a resource type gives each property's unit ----

# A DMTF JSON Schema file: from ./schemas, else downloaded there once; {} when it isn't published (Oem types)
@lru_cache(maxsize=None)
def schema_file(name) -> dict:
    with _schema_lock:
        try:
            return json.loads((SCHEMAS / name).read_text())
        except OSError:
            return download(name)


# Download a schema file from redfish.dmtf.org into ./schemas; {} when it can't
def download(name) -> dict:
    try:
        r = requests.get(DMTF + name, timeout=15)
        r.raise_for_status()
        doc = r.json()
    except (requests.RequestException, ValueError) as e:  # a 404 is an Oem type; anything else is worth a warning
        level = logging.DEBUG if isinstance(e, requests.HTTPError) else logging.WARNING
        log.log(level, "schema %s: %s", name, reason(str(e)))
        return {}
    SCHEMAS.mkdir(exist_ok=True)
    (SCHEMAS / name).write_text(json.dumps(doc))
    return doc


# {property: unit} for every property with a unit in the type's schema
@lru_cache(maxsize=None)
def schema_units(name, version) -> dict[str, str]:
    """Without a version (iLO 4): the newest versioned schema the unversioned file lists."""
    if version:
        file = f"{name}.{version}.json"
    else:
        listed = re.findall(rf"{re.escape(name)}\.v\d+_\d+_\d+\.json", json.dumps(schema_file(name + ".json")))
        file = listed[-1] if listed else name + ".json"
    schema = schema_file(file)
    return {p: s["units"] for d in (schema.get("definitions") or {}).values()
            for p, s in (obj(d).get("properties") or {}).items() if "units" in obj(s)}


# ---- Telemetry: every numeric property with a unit that measures something ----

# (regex, MetricDefinition) per MetricProperties URI, each {wildcard} limited to its Wildcards values
def metric_definitions(found) -> list[tuple[re.Pattern, dict]]:
    out = []
    for _, d in of(found, "MetricDefinition"):
        values = {w.get("Name"): w.get("Values") or ["*"] for w in d.get("Wildcards") or []}
        # lambda m: a {Name} wildcard match -> a regex alternation of its values; "*" matches any one path segment
        sub = lambda m: "(?:%s)" % "|".join("[^/#]+" if v == "*" else re.escape(v) for v in values.get(m[1], ["*"]))
        out += [(re.compile(WILDCARD.sub(sub, re.escape(p))), d) for p in d.get("MetricProperties") or []]
    return out


# The MetricDefinition whose MetricProperties match a metric URI; {} when none does
def definition(definitions, metric) -> dict:
    # lambda rd: a (regex, definition) pair -> True when the regex matches the whole metric URI
    return next(filter(lambda rd: rd[0].fullmatch(metric), definitions), (None, {}))[1]


# A record for one numeric property (keys: kind and FIELDS); kind is None for a number that measures nothing
def reading(uri, doc, definitions, path, value) -> dict:
    """Its unit comes from the BMC's MetricDefinition for it, else the DMTF schema, else a sibling '<Property>Units'
    (Sensor.ReadingUnits), else, for a Sensor's threshold (Thresholds/UpperCritical/Reading), the sensor's own unit.
    What it measures comes from the unit, else from the Sensor's ReadingType; Role says reading or limit. Name, Status
    and Critical are those of the nearest named object holding it (the sensor, fan or power supply)."""
    keys = path.split("/")
    name, version = rtype(doc)
    md = definition(definitions, f"{uri}#/{path}")
    # lambda i: a path length, longest first -> True when that much of the path leads to a named object
    i = next(filter(lambda i: named(at(doc, keys[:i])), range(len(keys) - 1, -1, -1)), 0)
    item, prop, last = obj(at(doc, keys[:i])), "/".join(keys[i:]), keys[-1]
    unit = md.get("Units") or schema_units(name, version).get(last) or obj(at(doc, keys[:-1])).get(last + "Units")
    kind = KIND.get(unit)
    if last == "Reading":  # a Sensor, or one of its thresholds: the sensor's own unit and ReadingType
        unit = unit or item.get("ReadingUnits")
        kind = kind or KIND.get(unit) or item.get("ReadingType")
    flat = properties(item)
    return {"kind": kind, "resource": pointer(uri, keys[:i]), "name": named(item), "property": prop,
            "role": "limit" if LIMIT.search(prop) else "reading", "value": value,
            "unit": unit, "critical": flat.get(choose(flat, CRITICAL)), "precision": md.get("Precision"),
            "definition": md.get("Id"), "health": obj(item.get("Status")).get("Health"),
            "state": obj(item.get("Status")).get("State"), "source": name}


# Every reading and every limit (threshold, range, capacity...) of one host, as records
def readings(found) -> list[dict]:
    """Also those null right now (absent or powered-off sensors); numbers that measure nothing are left out."""
    definitions = metric_definitions(found)
    # lambda d: a document -> its (path, value) leaf properties whose value is a number or null
    # lambda pv: a (path, value) pair -> True when the value is a number or null
    numbers = lambda d: filter(lambda pv: type(pv[1]) in (int, float, type(None)), properties(d).items())
    # lambda r: a record -> True when it measures something (it has a kind)
    return list(filter(lambda r: r["kind"], (reading(u, d, definitions, p, v) for u, d in found.items()
                                             for p, v in numbers(d))))


# How far a reading is below its critical threshold; None when either is missing
def margin(r) -> float | None:
    """A critical of 0 means no threshold (iLO 4 reports 0 for sensors without one)."""
    try:
        return round((r.get("critical") or None) - r.get("value"), 2)
    except TypeError:
        return None


# ---- Detail report sections: each returns a list of tables or lines; empty ones are dropped ----

# One Property/Value table per resource of a type; title may use {uri}
def props(found, title, name, rows) -> list:
    # lambda h, row: a column header and a (property, value) row -> the property, which styles the value cell
    return [block(title.format(uri=u), PROPS, rows(d), key=lambda h, row: row[0]) for u, d in of(found, name)]


# One table of the items of some types, a column per property path ('A|B': A, else B when A isn't real)
def listing(found, title, sources, columns) -> list:
    # lambda u, i: an item's URI and document -> its leaf properties, plus the URI as "@resource"
    flat = lambda u, i: {"@resource": u, **properties(i)}
    rows = [[text(f.get(choose(f, c))) for c in columns]
            for s in sources for u, i in items(found, s) for f in [flat(u, i)]]
    return [block(title, [HEADERS.get(c, c.split("|")[0]) for c in columns], rows)]


# Vendor extensions: every Oem property, which the Redfish schema doesn't standardize
def vendor_oem(found, title) -> list:
    # lambda p: an Oem property path -> (path before Oem, vendor, vendor property)
    split = lambda p: OEM_PATH.fullmatch(p).groups()
    rows = sorted((vendor, u, "/".join(filter(None, (before, prop))) or "-", v)
                  for u, d in found.items() for p, v in details(d, oem=True) for before, vendor, prop in [split(p)])
    return [block(title, ("Vendor", "Resource", "Property", "Value"), rows,
                  border_style="magenta", title_style="bold magenta", row_styles=["italic"])]


# One Property/Value table per service: every resource whose type ends in Service
def services(found) -> list:
    # lambda ud: a (uri, document) pair -> True when the document's type ends in Service
    offered = filter(lambda ud: rtype(ud[1])[0].endswith("Service"), sorted(found.items()))
    # lambda h, row: a column header and a (property, value) row -> the property, which styles the value cell
    return [block(f"{rtype(d)[0]} · {u}", PROPS, details(d), key=lambda h, row: row[0]) for u, d in offered]


# Every operation a resource offers: {(resource, action name): {property: value}}, standard and Oem
def action_list(found) -> dict[tuple[str, str], dict]:
    grouped = {}
    for u, d in sorted(found.items()):
        for p, v in properties(d).items():
            for name, prop in ACTION.findall(p):
                grouped.setdefault((u, name), {})[prop] = v
    return grouped


# One table of every action: resource, name, target URI and parameters
def actions(found, title) -> list:
    rows = [(u, name, text(props.get("target")), parameters(found, props))
            for (u, name), props in action_list(found).items()]
    return [block(title, ("Resource", "Action", "Target", "Parameters"), rows)]


# The parameters of an action and their allowed values, from the action and its ActionInfo: "Param: a, b; ..."
def parameters(found, action) -> str:
    allowed = {}
    for key, value in action.items():
        for param in ALLOWED.findall(key):
            allowed.setdefault(param, []).append(text(value))
    info = found.get(norm(text(action.get("@Redfish.ActionInfo"))), {})
    for param in info.get("Parameters") or []:
        allowed.setdefault(param.get("Name"), []).extend(map(text, param.get("AllowableValues") or []))
    return "; ".join(f"{p}: {', '.join(dict.fromkeys(v)) or 'any'}" for p, v in allowed.items()) or "-"


# (resource, type, name, health, health rollup, state) for every object that has a Status, values as reported
def components(found) -> list[tuple[str, ...]]:
    return [(pointer(u, keys), rtype(d)[0], text(named(item)), *discrete(item))
            for u, d in sorted(found.items())
            for path in dict.fromkeys(m[1] or "" for m in filter(None, map(STATUS_LEAF.match, properties(d))))
            for keys in [list(filter(None, path.split("/")))] for item in [obj(at(d, keys))]]


# Every object with a Status and all its discrete values; Critical and Warning first
def health(found, title) -> list:
    comps = components(found)
    return [Text(f"{title}: {len(comps)} with a status · Health: {tally(c[3] for c in comps)} · "
                 f"State: {tally(c[5] for c in comps)}"),
            block("Components", ("Resource", "Type", "Name", "Health", "HealthRollup", "State"),
                  # lambda c: a component -> its sort key: Critical, then Warning, then the rest, each by resource
                  sorted(comps, key=lambda c: (SEVERITY.get(c[3], 2), c)))]


# Every numeric property with a measuring unit, one table per kind of measurement
def measurements(found, title) -> list:
    rows = sorted((tuple(map(text, map(r.get, FIELDS))), r["kind"]) for r in readings(found))
    # lambda rk: a (row, kind) pair -> True when the row is of this table's kind
    return [block(f"{title} · {kind}", READINGS, [row for row, _ in filter(lambda rk: rk[1] == kind, rows)])
            for kind in dict.fromkeys([*KIND.values(), *(k for _, k in rows)])]


# The detail report: per part, its sections as (section function, arguments...)
INVENTORY = (
    (props, "System · {uri}", "ComputerSystem", partial(pick, fields=SYSTEM)),
    (listing, "Processors", ("Processor",),  # AMI files the CPU name under ProcessorId/EffectiveFamily, not Model
     ("@resource", "Id", "Manufacturer", "Model|ProcessorId/EffectiveFamily", "ProcessorType", "TotalCores",
      "TotalThreads", "MaxSpeedMHz", "Status/Health", "Status/State")),
    (listing, "Memory", ("Memory",), ("@resource", "Id", "CapacityMiB", "MemoryDeviceType", "OperatingSpeedMhz",
                                       "Manufacturer", "PartNumber", "SerialNumber", "Status/Health", "Status/State")),
    (listing, "Storage controllers", ("StorageController", "Storage/StorageControllers"),
     ("@resource", "Name", "Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "Status/Health",
      "Status/State")),
    (listing, "Drives", ("Drive", "SimpleStorage/Devices"),
     ("@resource", "Name", "Manufacturer", "Model", "SerialNumber", "Revision", "CapacityBytes", "MediaType",
      "Protocol", "PredictedMediaLifeLeftPercent", "Status/Health", "Status/State")),
    (listing, "Volumes", ("Volume",), ("@resource", "Name", "RAIDType", "CapacityBytes", "Status/Health",
                                       "Status/State")),
    (listing, "Ethernet interfaces", ("EthernetInterface",),
     ("@resource", "Id", "MACAddress", "IPv4Addresses/0/Address", "SpeedMbps", "LinkStatus", "Status/Health",
      "Status/State")),
    (listing, "Network adapters", ("NetworkAdapter",),
     ("@resource", "Id", "Manufacturer", "Model", "PartNumber", "SerialNumber", "Controllers/0/FirmwarePackageVersion",
      "Status/Health", "Status/State")),
    (props, "Manager · {uri}", "Manager", partial(pick, fields=MANAGER)),
    (props, "Chassis · {uri}", "Chassis", partial(pick, fields=CHASSIS)),
    (listing, "Power supplies", ("PowerSupply", "Power/PowerSupplies"),
     ("@resource", "Name", "Manufacturer", "Model", "SerialNumber", "FirmwareVersion", "PowerCapacityWatts",
      "Status/Health", "Status/State")),
    (listing, "Firmware", ("SoftwareInventory",), ("Name", "Version", "Updateable", "Status/Health", "Status/State")),
    (vendor_oem, "Vendor Oem"),
)
SERVICES = (
    (props, "Redfish service", "ServiceRoot", partial(pick, fields=SERVICE)),
    (services,),
    (props, "Network services · {uri}", "ManagerNetworkProtocol", details),
)
ACTIONS_ = (
    (actions, "Actions"),
)
HEALTH = (
    (listing, "Failed requests", ("(untyped)",), ("@resource", "error")),
    (health, "Components"),
)
TELEMETRY = (
    (measurements, "Measurements"),
    (props, "Metric report definition · {uri}", "MetricReportDefinition", details), # metadata for the metric properties
    (listing, "Metric reports", ("MetricReport/MetricValues",), # Generated reports
     ("@resource", "MetricId", "MetricProperty", "MetricValue", "Timestamp")),
    (props, "Trigger · {uri}", "Triggers", details),
)
PARTS = (("Inventory", INVENTORY), ("Services", SERVICES), ("Actions", ACTIONS_), ("Health", HEALTH),
         ("Telemetry", TELEMETRY))


# The full report of one host: every part, each a heading and its sections
def report(server, found) -> Group:
    parts = [x for part, sections in PARTS
             for x in (Rule(Text(part, style="bold cyan"), align="left"),
                       *(t for section, *args in sections for t in section(found, *args)))]
    return Group(Rule(Text("Host " + " · ".join(label(server)), style="bold")), *filter(None, parts))


# ---- Collect: crawl every BMC and snapshot each host ----

# One row of the collect summary: connection, resources, errors and identity of a host
def summary(server, found, seconds) -> tuple[str, ...]:
    root = found.get(ROOT, {}) # Root service /redfish/v1
    errors = [d.get("error", "no @odata.type") for _, d in of(found, "(untyped)")]
    return (*label(server), connection(server), str(len(found)), str(len(errors)), f"{seconds:.1f}",
            text(root.get("RedfishVersion")), *identity(found), rollup(found),
            server.get("stopped") or next(iter(errors), "-"))


# The collect summary table, with the fleet totals as caption
def overview(rows) -> Table | list:
    col = {h: [r[i] for r in rows] for i, h in enumerate(SUMMARY)}
    connected, stopped = col["Connection"].count("Connected"), col["Connection"].count("Stopped")
    caption = (f"{len(rows)} HOSTs · {connected} connected · {stopped} stopped · "
               f"{len(rows) - connected - stopped} failed · "
               f"{sum(map(int, col['Resources']))} resources · {sum(map(int, col['Errors']))} request errors")
    return block("Summary", SUMMARY, rows, caption=caption, caption_justify="left")


# Crawl one host into ./snapshots/<host>.json; the snapshot before it moves to ./snapshots/previous
def snapshot(server, tunnel) -> dict:
    start, found, stopped = time.monotonic(), {}, None
    tunnel[2]["until"] = start + tunnel[2]["deadline"]
    try:
        location = login(tunnel)
        try:
            discover(tunnel, ROOT, found)
        finally:
            if location:
                logout(tunnel, location)
    except ConnectionAbortedError as e:  # raised by call(): what was found so far is kept, the views say why
        stopped = str(e)
        log.warning("%s: %s", server["host"], stopped)
    seconds = round(time.monotonic() - start, 1)
    log.info("%s: %d resources in %.0fs", server["host"], len(found), seconds)
    snap = {"host": server["host"], "vendor": server.get("vendor", "-"), "project": server.get("project", "-"),
            "collected": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": seconds,
            "stopped": stopped, "resources": found}
    PREVIOUS.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(server["host"])
    if path.exists():
        path.replace(PREVIOUS / path.name)  # the crawl before, for the diff view
    path.write_text(json.dumps(snap))
    return snap


# Every server of the inventory, the inventory's defaults filled in
def load_servers(path) -> list[dict]:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return [{**(cfg.get("defaults") or {}), **server} for server in cfg["servers"]]


# Collect - run the poller and create snapshots
def collect(args) -> list:
    servers = load_servers(args.config)
    tunnels = [tunnel(server) for server in servers]  # fails on missing credentials before any request
    with ThreadPoolExecutor(len(servers)) as pool:
        snaps = list(pool.map(snapshot, servers, tunnels))
    # lambda s: a snapshot -> its summary row
    return [overview(per_host(lambda s: summary(s, s["resources"], s["seconds"]), snaps))]


# ---- Views: each reads the snapshots and returns what it shows ----

# View health of exposed components
def view_health(args) -> list:
    """Per host a count of every Health value reported, then every component whose Health is Warning or Critical
    and every failed request across the fleet. Values exactly as the BMCs report them."""
    snaps = load()
    # lambda s: a snapshot -> (host, its components, each prefixed with the host)
    comps = dict(per_host(lambda s: (s["host"], [(s["host"], *c) for c in components(s["resources"])]), snaps))
    # lambda v: a Health value -> its sort key: Critical, Warning, then the others alphabetically
    values = sorted({c[4] for cs in comps.values() for c in cs}, key=lambda v: (-SEVERITY.get(v, -1), v))
    # lambda s: a snapshot -> (host, its jobs to check, each prefixed with the host)
    jobs = dict(per_host(lambda s: (s["host"], [(s["host"], *j) for j in open_jobs(s["resources"])]), snaps))
    # lambda s: a snapshot -> its row: label, connection, identity, rollup, a count per Health value, jobs, failures
    hosts = per_host(lambda s: (*label(s), connection(s), *identity(s["resources"]), rollup(s["resources"]),
                                *(str(Counter(c[4] for c in comps.get(s["host"], []))[v]) for v in values),
                                str(len(jobs.get(s["host"], []))), str(len(of(s["resources"], "(untyped)"))),
                                s["collected"]), snaps)
    # lambda c: a component -> True when its Health is Critical or Warning; the key sorts Critical first
    problems = sorted((c for cs in comps.values() for c in filter(lambda c: c[4] in SEVERITY, cs)),
                      key=lambda c: (SEVERITY[c[4]], c))
    failed = [(s["host"], u, d.get("error", "-")) for s in snaps for u, d in of(s["resources"], "(untyped)")]
    # lambda s: a snapshot -> True when its crawl was stopped
    failed += [(s["host"], "(crawl stopped)", s["stopped"]) for s in filter(lambda s: s.get("stopped"), snaps)]
    return [
        block("Health by host", ("Host", "Vendor", "Project", "Connection", "Manufacturer", "Model", "HealthRollup",
                                 *(f"Health {v}" for v in values), "Jobs to check", "Failed requests",
                                 "Collected"), hosts),
        block("Not OK", ("Host", "Resource", "Type", "Name", "Health", "HealthRollup", "State"), problems)
        or Text("Not OK: none"),
        block("Jobs to check", JOBS_ROW, [j for js in jobs.values() for j in js]) or Text("Jobs to check: none"),
        block("Failed requests", ("Host", "Resource", "Error"), failed) or Text("Failed requests: none")]


# Jobs and tasks that haven't ended, or ended badly: (resource, name, state, status, message, time), as reported
def open_jobs(found) -> list[tuple[str, ...]]:
    """Any JobState or TaskState but Completed (New, Pending, Running, Exception, Killed, Cancelled...), a Warning or
    Critical JobStatus or TaskStatus, or a message with a Warning or Critical MessageSeverity (Severity before
    Message v1_1). The message shown is the most severe one. A rollout's pre-flight skips a host that has any."""
    rows = []
    for u, d in sorted(found.items()):
        if rtype(d)[0] not in JOB_TYPES:
            continue
        state, status = d.get("JobState") or d.get("TaskState"), d.get("JobStatus") or d.get("TaskStatus")
        messages = [m for m in d.get("Messages") or [] if isinstance(m, dict)]
        # lambda m: a message -> its rank: Critical 0, Warning 1, anything else 2, so min() is the most severe
        worst = min(messages, key=lambda m: SEVERITY.get(m.get("MessageSeverity") or m.get("Severity"), 2), default={})
        severity = worst.get("MessageSeverity") or worst.get("Severity")
        if state != "Completed" or status in SEVERITY or severity in SEVERITY:
            rows.append((u, text(d.get("Name")), text(state), text(status), text(worst.get("Message")),
                         text(d.get("EndTime") or d.get("StartTime"))))
    return rows


# {component: (version, note)} for one host; the note describes the component's other images
def firmware(found) -> dict[str, tuple[str | None, str]]:
    """BIOS and BMC as the System and Manager report them, every entry of the firmware inventory (AMI lists BIOS there
    without a version), and the firmware of drives, controllers, NICs and PSUs per model."""
    parts = {}
    for prefix, source, model, version in FW_PARTS:
        for _, item in items(found, source):
            flat = properties(item)
            # lambda pv: a (path, value) pair -> True when the path is this part's version property
            for v in filter(real, (v for p, v in filter(lambda pv: re.fullmatch(version, pv[0]), flat.items()))):
                parts.setdefault(f"{prefix} {text(flat.get(choose(flat, model)))}", set()).add(text(v))
    return {"System BIOS": (first_of(found, "ComputerSystem").get("BiosVersion"), ""),
            "Manager (BMC)": (first_of(found, "Manager").get("FirmwareVersion"), ""),
            **images(found),
            **{part: (" ".join(sorted(versions)), "") for part, versions in parts.items()}}


# {name: (running version, note)} per firmware component
def images(found) -> dict[str, tuple[str | None, str]]:
    return dict(component(group) for group in inventory_groups(found).values())


# {name: images} per firmware component: the inventory entries sharing a SoftwareId, as (linked as active, entry)
def inventory_groups(found) -> dict[str, list[tuple[bool, dict]]]:
    """Entries without a SoftwareId are grouped by Name. A manager's Links.ActiveSoftwareImage marks its running image
    for BMCs that don't report Active on the entries."""
    linked = {obj(obj(d.get("Links")).get("ActiveSoftwareImage")).get("@odata.id") for _, d in of(found, "Manager")}
    groups = {}
    for u, d in of(found, "SoftwareInventory"):
        groups.setdefault(d.get("SoftwareId") or d.get("Name") or u, []).append((u in linked, d))
    return {component(group)[0]: group for group in groups.values()}


# {row name: SoftwareId} for the firmware inventory rows whose entries report one
def software_ids(found) -> dict[str, str]:
    ids = {name: next(filter(None, (d.get("SoftwareId") for _, d in group)), None)
           for name, group in inventory_groups(found).items()}
    return {name: sid for name, sid in ids.items() if sid}


# A model's baseline for one host: a line keyed SoftwareId:<id> applies to the row whose entries carry that id
def host_baseline(baseline, ids) -> dict[str, str]:
    """A SoftwareId stays the same across hosts and firmware versions where a Name may not (some BMCs put a port's MAC
    address in a NIC's name). A line under the row's own name wins; a SoftwareId line no row carries stays as it is."""
    out = {k: v for k, v in baseline.items() if not k.startswith("SoftwareId:")}
    matched = set()
    for name, sid in ids.items():
        key = f"SoftwareId:{sid}"
        if key in baseline:
            out.setdefault(name, baseline[key])
            matched.add(key)
    return {**out, **{k: v for k, v in baseline.items() if k.startswith("SoftwareId:") and k not in matched}}


# One component's (name, (running version, note)) from its images, as (linked as active, entry) pairs
def component(group) -> tuple[str, tuple[str | None, str]]:
    """The running image has Active true or ImageState Active (SoftwareInventory v1_12, v1_15), or is a manager's
    ActiveSoftwareImage. Without one, a component whose images all carry one version runs that version; with several
    versions which one runs isn't reported, so the version is None and the note lists them. The note also names each
    other image by its state, and gives the LowestSupportedVersion: the oldest version a rollback can go to."""
    running, rest = [], []
    for linked, d in group:
        (running if linked or d.get("Active") is True or d.get("ImageState") == "Active" else rest).append(d)
    versions = list(dict.fromkeys(text(d.get("Version")) for _, d in group))
    version = (running[0] if running else group[0][1]).get("Version") if running or len(versions) == 1 else None
    notes = [f"{image_state(d) or 'Inactive'} {text(d.get('Version'))}" for d in rest if running or image_state(d)]
    if version is None and len(versions) > 1:
        notes.insert(0, "running not reported: " + ", ".join(versions))
    lowest = next(filter(None, (d.get("LowestSupportedVersion") for _, d in group)), None)
    if lowest:
        notes.append(f"lowest supported {lowest}")
    return text((running or [group[0][1]])[0].get("Name")), (version, " · ".join(notes))


# The state of a firmware image that isn't running: ImageState (v1_15), Staged or Armed, Inactive when Active is false
def image_state(d) -> str | None:
    if d.get("ImageState"):
        return d["ImageState"]
    if d.get("Staged") or d.get("Armed"):
        return "Staged" if d.get("Staged") else "Armed"
    return "Inactive" if d.get("Active") is False else None


# Components x hosts of one model, checked against the model's baseline, or else against each other
def matrix(title, rows) -> Table:
    """rows: ((host, vendor, project), {component: (version, note)}, that host's baseline). A component in a baseline
    gets its approved version in the Baseline column and each host's version green when it matches, red when not; any
    other component highlights a version that differs from the most common one. Notes (the component's other images)
    go under the version."""
    baseline = {c: want for _, _, bl in rows for c, want in bl.items()}
    components = list(dict.fromkeys([*(c for _, fw, _ in rows for c in fw), *baseline]))
    cells = {c: [(*fw.get(c, (None, "")), bl.get(c)) for _, fw, bl in rows] for c in components}
    same = sum(len({text(v) for v, _, _ in cs}) == 1 for cs in cells.values())
    on = sum(all(text(fw.get(c, (None, ""))[0]) == want for c, want in bl.items()) for _, fw, bl in rows)
    hosts = (f"{host}\n{project}" for (host, _, project), _, _ in rows)
    headers = ("Component", *(["Baseline"] if baseline else []), *hosts)
    caption = f"{len(rows)} hosts · {same} of {len(components)} components the same on every host"
    t = Table(*(Column(h, overflow="fold", min_width=len(h.split("\n")[0])) for h in headers), title=title,
              title_justify="left", title_style="bold", expand=True, caption_justify="left",
              caption=caption + (f" · {on} of {len(rows)} hosts on baseline" if baseline else ""))
    for c, cs in cells.items():
        common = Counter(text(v) for v, _, _ in cs).most_common(1)[0][0]
        t.add_row(Text(c), *([Text(text(baseline.get(c)))] if baseline else []),
                  *(version_cell(text(v), note, want, common) for v, note, want in cs))
    return t


# One host's version of a component: green on baseline, red off it, yellow when it differs from the most common one
def version_cell(version, note, want, common) -> Text:
    if want is not None:
        style = "green" if version == want else "bold red"
    else:
        style = "bold yellow" if version != common else ""
    cell = Text(version, style=style)
    if note:
        cell.append("\n" + note, style="dim")
    return cell


# One row of the compliance table: how many baseline components a host has at the approved version, and the rest
def compliance(host, model, fw, baseline) -> tuple[str, ...]:
    have = {c: text(fw.get(c, (None, ""))[0]) for c in baseline}
    reported = {c: have[c] if fw.get(c, (None, ""))[0] is not None else "not reported" for c in baseline}
    needs = [f"{c}: {reported[c]} → {want}" for c, want in baseline.items() if have[c] != want]
    return (*host, model, f"{len(baseline) - len(needs)}/{len(baseline)}", "\n".join(needs) or "-")


# {model: {component: approved version}} from baseline.yaml, every value read as text (2.20 stays 2.20); {} without
def load_baseline(path=BASELINE) -> dict[str, dict[str, str]]:
    try:
        return yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader) or {}
    except FileNotFoundError:
        return {}


# View firmware - baseline compliance per host, then one matrix per hardware model
def view_firmware(args) -> list:
    baselines, groups = load_baseline(), {}
    # lambda s: a snapshot -> (its model, (its label, its firmware, its baseline))
    for model, row in per_host(lambda s: firmware_row(s, baselines), reachable()):
        groups.setdefault(model, []).append(row)
    checked = [compliance(host, model, fw, bl)
               for model, rows in sorted(groups.items()) if baselines.get(model) for host, fw, bl in rows]
    on, baseline = sum(r[-1] == "-" for r in checked), os.path.relpath(BASELINE)
    return [block("Baseline compliance", COMPLIANCE_ROW, checked, caption_justify="left",
                  caption=f"{on} of {len(checked)} hosts on baseline · models without one in {baseline}: "
                          f"{', '.join(sorted(set(groups) - set(baselines))) or 'none'}")
            or Text(f"Baseline compliance: no model of the fleet is in {baseline}"),
            *(matrix(model, rows) for model, rows in sorted(groups.items()))]


# One host of the firmware view: (its model, (its label, its firmware, its baseline with SoftwareId lines applied))
def firmware_row(snap, baselines) -> tuple[str, tuple]:
    found = snap["resources"]
    model = " ".join(identity(found))
    return model, (label(snap), firmware(found), host_baseline(baselines.get(model) or {}, software_ids(found)))


# One CMDB row: identity, CPUs, GPUs, memory, drives, NICs and power supplies of one host
def inventory(snap) -> tuple[str, ...]:
    found = snap["resources"]
    system = properties(first_of(found, "ComputerSystem"))
    processors = [properties(d) for _, d in of(found, "Processor")]
    # lambda p: a processor -> True when it is a CPU (or has no type); the next one: True when it is a GPU
    cpus = list(filter(lambda p: p.get("ProcessorType") in (None, "CPU"), processors))
    gpus = list(filter(lambda p: p.get("ProcessorType") == "GPU", processors))
    cpu = next(filter(real, [system.get("ProcessorSummary/Model"),
                             *(p.get(choose(p, "Model|ProcessorId/EffectiveFamily")) for p in cpus)]), None)
    dimms = [d.get("CapacityMiB") or 0 for _, d in of(found, "Memory")]
    sizes = (d.get("CapacityBytes") or 0 for _, d in of(found, "Drive"))
    # lambda b: a drive size in bytes -> True from 1 GB up: leaves out virtual media (AMI's virtual CD-ROM)
    disks = list(filter(lambda b: b >= 1e9, sizes))
    psus = [i for _, i in items(found, "PowerSupply") + items(found, "Power/PowerSupplies")]
    # lambda u: an EthernetInterface URI -> True when it belongs to the host (not to the BMC)
    host_nics = filter(lambda u: u.startswith(ROOT + "/Systems/"), dict(of(found, "EthernetInterface")))
    # lambda i: a power supply -> True when it is present (State isn't Absent)
    present = list(filter(lambda i: obj(i.get("Status")).get("State") != "Absent", psus))
    return (*label(snap), *identity(found), first_real(found, "SerialNumber"), text(system.get("UUID")),
            f"{system.get('ProcessorSummary/Count') or len(cpus)} × {text(cpu)}",
            text(sum(p.get("TotalCores") or 0 for p in cpus) or None), str(len(gpus)),
            text(system.get("MemorySummary/TotalSystemMemoryGiB")),
            f"{sum(1 for d in dimms if d)}/{len(dimms)}" if dimms else "-",
            f"{len(disks)} · {sum(disks) / 1e12:.1f} TB" if disks else "-",
            str(len(of(found, "NetworkAdapter")) or len(list(host_nics))),
            f"{len(present)}/{len(psus)}")


# View inventory - one row per reachable host
def view_inventory(args) -> list:
    snaps, hosts = reachable(), [s["host"] for s in load()]
    missing = ", ".join(sorted(set(hosts) - {s["host"] for s in snaps})) or "none"
    return [block("Inventory", INVENTORY_ROW, per_host(inventory, snaps), caption_justify="left",
                  caption=f"{len(snaps)} of {len(hosts)} hosts · not (fully) collected: {missing}")]


# Telemetry per host: readings count, hottest temperature against its critical threshold, highest power reading
def hotspot(snap, rs) -> tuple[str, ...]:
    # lambda r: a record -> True when it is a reading (not a limit)
    rs = list(filter(lambda r: r["role"] == "reading", rs))
    # lambda kind: a kind -> the readings of that kind with a numeric value (the inner lambda r tests one reading)
    numeric = lambda kind: filter(lambda r: r["kind"] == kind and type(r["value"]) in (int, float), rs)
    # lambda r: a reading -> its value, to find the highest
    hot = max(numeric("Temperature"), key=lambda r: r["value"], default={})
    power = max((r["value"] for r in numeric("Power")), default=None)
    # lambda r: a reading -> True when its Health is Critical or Warning
    not_ok = list(filter(lambda r: r["health"] in SEVERITY, rs))
    return (*label(snap), str(len(rs)), text(hot.get("name")), text(hot.get("value")), text(hot.get("critical")),
            text(margin(hot)), text(power), str(len(not_ok)))


# One row per sensor reading: every kind, with or without a threshold (Critical and Margin are "-" without one)
def sensor_row(r) -> tuple[str, ...]:
    headroom = margin(r) if r["role"] == "reading" else None  # a margin only means something for a reading
    return (text(r["kind"]), r["resource"], text(r["name"]), r["property"], r["role"], text(r["value"]),
            text(r["unit"]), text(r["critical"]), text(headroom), text(r["health"]), text(r["state"]), r["source"])


# View telemetry - per host the hottest temperature and highest power, then every sensor of every host
def view_telemetry(args) -> list:
    # lambda s: a snapshot -> (the snapshot, its reading records)
    host_readings = per_host(lambda s: (s, readings(s["resources"])), reachable())
    # lambda r: a record -> its sort key: kind, resource, the reading before its limits, property
    order = lambda r: (text(r["kind"]), r["resource"], r["role"] != "reading", r["property"])
    return [block("Telemetry by host", TELEMETRY_ROW, [hotspot(s, rs) for s, rs in host_readings]),
            *filter(None, (block(f"Sensors · {' · '.join(label(s))}", SENSORS,
                                 list(map(sensor_row, sorted(rs, key=order)))) for s, rs in host_readings))]


# What a hardware model can do, from one of its hosts
def capabilities(model, snaps) -> tuple[str, ...]:
    snap = snaps[0]
    found = snap["resources"]
    types = sorted({rtype(d)[0] for d in found.values()})
    # lambda t: a resource type -> True when it is a service
    offered = ", ".join(t.removesuffix("Service") for t in filter(lambda t: t.endswith("Service"), types))
    acts = action_list(found)
    # lambda k: a (resource, action) key -> True for SimpleUpdate; the map's lambda k -> that action's parameters
    simple = map(lambda k: parameters(found, acts[k]), filter(lambda k: k[1] == "#UpdateService.SimpleUpdate", acts))
    update = first_of(found, "UpdateService")
    telemetry = (f"{len(of(found, 'MetricDefinition'))} metric, {len(of(found, 'MetricReportDefinition'))} report "
                 f"definitions · {len(of(found, 'Triggers'))} triggers")
    return (" ".join(model), str(len(snaps)), snap["host"], text(found.get(ROOT, {}).get("RedfishVersion")),
            str(len(types)), offered or "-", str(len(acts)), next(simple, "-"),
            ", ".join(filter(update.__contains__, ("HttpPushUri", "MultipartHttpPushUri"))) or "-",
            telemetry if first_of(found, "TelemetryService") else "-", str(len(of(found, "Sensor"))))


# View capabilities - one row per hardware model
def view_capabilities(args) -> list:
    groups = {}
    for snap in reachable():
        groups.setdefault(identity(snap["resources"]), []).append(snap)
    return [block("Capabilities by model", CAPABILITIES_ROW,
                  [capabilities(model, snaps) for model, snaps in sorted(groups.items())])]


# {(resource, property): value} without what changes on every crawl
def stable(found) -> dict[tuple[str, str], Any]:
    """Left out: annotations, numbers (readings, counters), clocks, and the metric reports."""
    # lambda pv: a (path, value) pair -> True when it is no annotation, no number and no clock
    keep = lambda pv: "@" not in pv[0] and type(pv[1]) not in (int, float) and not VOLATILE.search(pv[0])
    # lambda ud: a (uri, document) pair -> True when the document isn't a metric report
    docs = filter(lambda ud: rtype(ud[1])[0] != "MetricReport", found.items())
    return {(u, p): v for u, d in docs for p, v in filter(keep, properties(d).items())}


# (resource, property, before, after) between two crawls of one host
def changes(old, new) -> list[tuple[str, str, str, str]]:
    """Resources that came or went, then every property that changed on the resources both crawls have."""
    both = new.keys() & old.keys()
    before, after = (stable({u: f[u] for u in both}) for f in (old, new))
    # lambda k: a (resource, property) key -> True when its value differs between the crawls
    changed = filter(lambda k: before.get(k) != after.get(k), sorted(before.keys() | after.keys()))
    return ([(u, "(resource)", "-", "added") for u in sorted(new.keys() - old.keys())]
            + [(u, "(resource)", "present", "removed") for u in sorted(old.keys() - new.keys())]
            + [(u, p, text(before.get((u, p))), text(after.get((u, p)))) for u, p in changed])


# View diff - what changed since the previous collect
def view_diff(args) -> list:
    # lambda s: a snapshot -> (the snapshot, the one before it or {})
    pairs = per_host(lambda s: (s, previous(s["host"])), load())
    # lambda sp: a (snapshot, previous) pair -> True when there is a previous one
    compared = list(filter(lambda sp: sp[1], pairs))
    rows = [(s["host"], *c) for s, old in compared for c in changes(old["resources"], s["resources"])]
    since = f"{len(compared)} of {len(pairs)} hosts compared with their previous collect"
    return [block("Changes", ("Host", "Resource", "Property", "Before", "After"), rows, caption=since,
                  caption_justify="left") or Text(f"No changes · {since}")]


# View detail - the full report of one host
def view_detail(args) -> list:
    try:
        snap = json.loads(snapshot_path(args.host).read_text())
    except FileNotFoundError:
        sys.exit(f"no snapshot for {args.host}: run 'redfish_poller.py collect' first")
    return [report(snap, snap["resources"])]


# Parse the command, run it, print what it returns and write the files asked for
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    output = output_options()
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    # lambda name, run, help: add a view command `name` that runs the function `run`
    command = lambda name, run, help: commands.add_parser(name, parents=[output], help=help).set_defaults(run=run)
    c = commands.add_parser("collect", parents=[output], help="crawl every BMC in the inventory, snapshot each host")
    c.add_argument("config", nargs="?", default=INVENTORY_FILE, help="the BMCs [<site>/inventory.yaml]")
    c.set_defaults(run=collect)
    command("health", view_health, "what is broken right now: health, jobs to check, failed requests")
    command("firmware", view_firmware, "firmware per hardware model, against the baseline")
    command("inventory", view_inventory, "one row per host: what we have")
    command("telemetry", view_telemetry, "every sensor of every host, with its limits")
    command("capabilities", view_capabilities, "what each hardware model can do")
    command("diff", view_diff, "what changed since the previous collect")
    d = commands.add_parser("detail", parents=[output], help="everything about one host")
    d.add_argument("host")
    d.set_defaults(run=view_detail)
    args = p.parse_args()
    logging.Formatter.converter = time.gmtime  # log times in UTC, like rollout.py
    logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)  # retries end up in the summary instead
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning) # by default disabled
    show(args.run(args), args)


if __name__ == "__main__":
    main()
