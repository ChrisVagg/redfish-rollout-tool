#!/usr/bin/env python3
"""Redfish fleet report: one collector, several views.

  collect                   Crawl every BMC once, in parallel: one persistent HTTPS connection (tunnel) per BMC,
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
  exporter                  Crawl every BMC each --interval and serve the readings, health, firmware and BMC
                            resources as Prometheus metrics on --listen. One session per BMC, kept across crawls;
                            a host whose fresh login is refused is not asked again until a restart.
  detail HOST               Everything about one host: inventory, services, actions, health and telemetry. A
                            reading's unit comes from the BMC's MetricDefinitions, else the DMTF JSON Schema of the
                            resource (downloaded once into ./schemas), else the resource itself (ReadingUnits).

The site is the fleet the poller reads: SITE=prod (the default) or SITE=lab. Its hosts are NetBox's site of that name
(NETBOX_URL, read with NETBOX_TOKEN), and its folder holds the snapshots."""
import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache, partial
from math import inf
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests
import urllib3
import yaml
from rich.console import Group
from rich.rule import Rule
from rich.table import Column, Table
from rich.text import Text

from constants import (ACCOUNT, ACTION, ALLOWED, BASELINE, CAPABILITIES_ROW, CHASSIS, COMPLIANCE_ROW, CONNECTION,
                       CRITICAL, DMTF, FIELDS, FW_PARTS, INVENTORY_ROW, JOBS_ROW, JOB_TYPES, KIND, LIMIT, LINK, LINKS,
                       MANAGER, NETBOX_URL, NETWORK_PORT, OEM_PATH, PREVIOUS, PROPS, READINGS, ROOT, SCHEMAS, SENSORS,
                       SERVICE, SEVERITY, SITE, SKIP, STATUS_LEAF, SUMMARY, SYSTEM, TELEMETRY_ROW, TEMPLATES,
                       THRESHOLD, VOLATILE, WILDCARD)
from helpers import (at, block, choose, connection, details, discrete, first_of, first_real, identity, items, label,
                     link, load, named, norm, obj, of, output_options, per_host, pick, pointer, previous, properties,
                     reachable, real, reason, rollup, rtype, sample, show, snapshot_path, tally, text, uri_of)
from redfish import REFUSED, call, login, logout, tunnel

log = logging.getLogger("redfish")
_schema_lock = threading.Lock()


# Every Redfish resource a document links to, outside the SKIP branches and TEMPLATES annotations
def links(doc) -> list[str]:
    resource = dict(filter(lambda kv: kv[0] not in TEMPLATES, doc.items()))
    uris = map(norm, LINKS.findall(json.dumps(resource)))
    return sorted(set(filter(lambda u: u.startswith(ROOT) and not SKIP.search(u), uris)))


# Fetch uri and, recursively, every resource it links to that isn't fetched yet: {uri: document}
def discover(tunnel, uri, found) -> dict:
    found[uri] = call(tunnel, uri)
    for child in filter(lambda u: u not in found, links(found[uri])):
        discover(tunnel, child, found)
    return found


# A DMTF JSON Schema file from ./schemas, else downloaded there once; {} when it isn't published
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
    except (requests.RequestException, ValueError) as e:
        level = logging.DEBUG if isinstance(e, requests.HTTPError) else logging.WARNING
        log.log(level, "schema %s: %s", name, reason(str(e)))
        return {}
    SCHEMAS.mkdir(exist_ok=True)
    (SCHEMAS / name).write_text(json.dumps(doc))
    return doc


# The newest versioned schema file of a type: Sensor -> Sensor.v1_11_0.json
def newest(name) -> str:
    listed = re.findall(rf"{re.escape(name)}\.v\d+_\d+_\d+\.json", json.dumps(schema_file(name + ".json")))
    return listed[-1] if listed else name + ".json"


# {property: unit} from the type's schema; the newest version when the BMC reports none (iLO 4)
@lru_cache(maxsize=None)
def schema_units(name, version) -> dict[str, str]:
    schema = schema_file(f"{name}.{version}.json" if version else newest(name))
    return {p: s["units"] for d in (schema.get("definitions") or {}).values()
            for p, s in (obj(d).get("properties") or {}).items() if "units" in obj(s)}


# The unit DMTF's Sensor schema gives a ReadingType, for sensors without ReadingUnits; else None
@lru_cache(maxsize=None)
def reading_type_unit(reading_type) -> str | None:
    types = obj(obj(schema_file(newest("Sensor")).get("definitions")).get("ReadingType"))
    said = obj(types.get("enumLongDescriptions")).get(reading_type) or ""
    m = re.search(r"ReadingUnits`? (?:property shall contain(?: either)?|value shall be) [`'](.+?)[`']", said)
    return m[1] if m else None


# (regex, MetricDefinition) per MetricProperties URI, each {wildcard} limited to its values
def metric_definitions(found) -> list[tuple[re.Pattern, dict]]:
    out = []
    for _, d in of(found, "MetricDefinition"):
        values = {w.get("Name"): w.get("Values") or ["*"] for w in d.get("Wildcards") or []}
        sub = lambda m: "(?:%s)" % "|".join("[^/#]+" if v == "*" else re.escape(v) for v in values.get(m[1], ["*"]))
        out += [(re.compile(WILDCARD.sub(sub, re.escape(p))), d) for p in d.get("MetricProperties") or []]
    return out


# The MetricDefinition whose MetricProperties match a metric URI; {} when none does
def definition(definitions, metric) -> dict:
    return next(filter(lambda rd: rd[0].fullmatch(metric), definitions), (None, {}))[1]


# One numeric property as a record with its unit, kind, role and owning sensor; kind None if it measures nothing
def reading(uri, doc, definitions, path, value) -> dict:
    keys = path.split("/")
    name, version = rtype(doc)
    md = definition(definitions, f"{uri}#/{path}")
    i = next(filter(lambda i: named(at(doc, keys[:i])), range(len(keys) - 1, -1, -1)), 0)
    item, prop, last = obj(at(doc, keys[:i])), "/".join(keys[i:]), keys[-1]
    unit = md.get("Units") or schema_units(name, version).get(last) or obj(at(doc, keys[:-1])).get(last + "Units")
    kind = KIND.get(unit)
    if last == "Reading" or (not unit and "Threshold" in prop):
        unit = unit or item.get("ReadingUnits") or reading_type_unit(item.get("ReadingType"))
        kind = kind or KIND.get(unit) or item.get("ReadingType")
    flat = properties(item)
    return {"kind": kind, "resource": pointer(uri, keys[:i]), "name": named(item), "property": prop,
            "role": "limit" if LIMIT.search(prop) else "reading", "value": value,
            "unit": unit, "critical": flat.get(choose(flat, CRITICAL)), "precision": md.get("Precision"),
            "definition": md.get("Id"), "health": obj(item.get("Status")).get("Health"),
            "state": obj(item.get("Status")).get("State"), "source": name}


# Every reading and every limit (threshold, range, capacity...) of one host, as records
def readings(found) -> list[dict]:
    definitions = metric_definitions(found)
    numbers = lambda d: filter(lambda pv: type(pv[1]) in (int, float, type(None)), properties(d).items())
    return list(filter(lambda r: r["kind"], (reading(u, d, definitions, p, v) for u, d in found.items()
                                             for p, v in numbers(d))))


# How far a reading is below its critical threshold; None when either is missing or critical is 0
def margin(r) -> float | None:
    try:
        return round((r.get("critical") or None) - r.get("value"), 2)
    except TypeError:
        return None


# One Property/Value table per resource of a type; title may use {uri}
def props(found, title, name, rows) -> list:
    return [block(title.format(uri=u), PROPS, rows(d), key=lambda h, row: row[0]) for u, d in of(found, name)]


# One table of the items of some types, a column per property path ('A|B': A, else B when A isn't real)
def listing(found, title, sources, columns) -> list:
    flat = lambda u, i: {"@resource": u, **properties(i)}
    rows = [[text(f.get(choose(f, c))) for c in columns]
            for s in sources for u, i in items(found, s) for f in [flat(u, i)]]
    return [block(title, ["Resource" if c == "@resource" else c.split("|")[0] for c in columns], rows)]


# One table of every Oem property: the vendor extensions the Redfish schema doesn't standardize
def vendor_oem(found, title) -> list:
    split = lambda p: OEM_PATH.fullmatch(p).groups()
    rows = sorted((vendor, u, "/".join(filter(None, (before, prop))) or "-", v)
                  for u, d in found.items() for p, v in details(d, oem=True) for before, vendor, prop in [split(p)])
    return [block(title, ("Vendor", "Resource", "Property", "Value"), rows,
                  border_style="magenta", title_style="bold magenta", row_styles=["italic"])]


# One Property/Value table per service: every resource whose type ends in Service
def services(found) -> list:
    offered = filter(lambda ud: rtype(ud[1])[0].endswith("Service"), sorted(found.items()))
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


# An action's parameters and allowed values, from the action and its ActionInfo: 'Param: a, b; ...'
def parameters(found, action) -> str:
    allowed = {}
    for key, value in action.items():
        for param in ALLOWED.findall(key):
            allowed.setdefault(param, []).append(text(value))
    info = found.get(norm(text(action.get("@Redfish.ActionInfo"))), {})
    for param in info.get("Parameters") or []:
        allowed.setdefault(param.get("Name"), []).extend(map(text, param.get("AllowableValues") or []))
    return "; ".join(f"{p}: {', '.join(dict.fromkeys(v)) or 'any'}" for p, v in allowed.items()) or "-"


# (resource, type, name, health, health rollup, state) for every object that has a Status
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
                  sorted(comps, key=lambda c: (SEVERITY.get(c[3], 2), c)))]


# Every numeric property with a measuring unit, one table per kind of measurement
def measurements(found, title) -> list:
    rows = sorted((tuple(map(text, map(r.get, FIELDS))), r["kind"]) for r in readings(found))
    return [block(f"{title} · {kind}", READINGS, [row for row, _ in filter(lambda rk: rk[1] == kind, rows)])
            for kind in dict.fromkeys([*KIND.values(), *(k for _, k in rows)])]


INVENTORY = (
    (props, "System · {uri}", "ComputerSystem", partial(pick, fields=SYSTEM)),
    (listing, "Processors", ("Processor",),
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
    (props, "Metric report definition · {uri}", "MetricReportDefinition", details),
    (listing, "Metric reports", ("MetricReport/MetricValues",),
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


# One row of the collect summary: connection, resources, errors and identity of a host
def summary(server, found, seconds) -> tuple[str, ...]:
    root = found.get(ROOT, {})
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


# Crawl one host into its snapshot file; the snapshot before it moves to PREVIOUS for the diff view
def snapshot(server, tunnel) -> dict:
    found, seconds, stopped = crawl(server, tunnel)
    log.info("%s: %d resources in %.0fs", server["host"], len(found), seconds)
    snap = {"host": server["host"], "vendor": server.get("vendor", "-"), "project": server.get("project", "-"),
            "collected": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": seconds,
            "stopped": stopped, "resources": found}
    PREVIOUS.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(server["host"])
    if path.exists():
        path.replace(PREVIOUS / path.name)
    path.write_text(json.dumps(snap))
    return snap


# One crawl of a host: (resources, seconds, why it stopped); keep reuses the tunnel's session
def crawl(server, tunnel, deadline=None, keep=False) -> tuple[dict, float, str | None]:
    start, found, stopped = time.monotonic(), {}, None
    tunnel[2].update(until=start + min(tunnel[2]["deadline"], deadline or tunnel[2]["deadline"]), failures=0)
    try:
        location = None if keep else login(tunnel)
        try:
            discover(tunnel, ROOT, found)
        finally:
            if location:
                logout(tunnel, location)
    except ConnectionAbortedError as e:
        stopped = str(e)
        log.warning("%s: %s", server["host"], stopped)
    return found, round(time.monotonic() - start, 1), stopped


# A site's servers, from NetBox, the inventory's only source: each device of the site with its BMC at its out-of-band
# IP, its manufacturer the vendor, its tenant the project, its rack, its tags canary and writable, its name the node,
# and how its BMC is reached from its config context (its site's and its platform's, merged by NetBox). Read with
# NETBOX_TOKEN, the service's read-only API token
def load_servers(url=NETBOX_URL, site=SITE.name) -> list[dict]:
    if not (token := os.environ.get("NETBOX_TOKEN")):
        raise SystemExit("NETBOX_TOKEN isn't set: make sets it from control/netbox/.tokens.env, which make control-up "
                         "makes")
    servers, page = [], f"{url.rstrip('/')}/api/dcim/devices/?site={site}&limit=1000"
    while page:
        try:
            r = requests.get(page, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            r.raise_for_status()
            body = r.json()  # something else than NetBox at the URL answers a page, not JSON
        except requests.RequestException as e:
            raise SystemExit(f"NetBox at {url}: {reason(str(e))}. make control-up starts it")
        for d in body["results"]:
            # only how its BMC is reached: a context can't make a host canary or writable, only a tag can
            context = {k: v for k, v in (d.get("config_context") or {}).items() if k in CONNECTION}
            account = f"{context.get('username_env')} {context.get('password_env')}"
            if not d["oob_ip"]:
                log.warning("NetBox: %s has no out-of-band IP, so its BMC isn't read", d["display"])
            elif not ACCOUNT.fullmatch(account) or not set(account.split()) <= os.environ.keys():
                log.warning("NetBox: %s has no BMC account to use: its config context must name X_USERNAME and "
                            "X_PASSWORD, both set here, so its BMC isn't read", d["display"])
            elif context.get("scheme", "https") != "https":
                log.warning("NetBox: %s isn't reached over HTTPS, so its BMC isn't read: its account would cross in "
                            "clear", d["display"])
            else:
                host, tags = d["oob_ip"]["address"].split("/")[0], {t["slug"] for t in d["tags"]}
                server = {"host": host, "vendor": d["device_type"]["manufacturer"]["name"],
                          "project": (d["tenant"] or {}).get("name"), "rack": (d["rack"] or {}).get("name"),
                          "canary": "canary" in tags or None, "writable": "writable" in tags or None,
                          "node": d["name"] if d["name"] != host else None}
                # unless its context says otherwise, HTTPS with the certificate checked: every reader takes the entry
                servers.append({"scheme": "https", "verify": True, **context,
                                **{k: v for k, v in server.items() if v is not None}})
        page = body["next"]
    return servers


# Crawl every BMC in parallel, snapshot each host and return the summary table
def collect(args) -> list:
    if not (servers := load_servers()):
        raise SystemExit(f"no BMC in NetBox site {SITE.name}: its hosts are added in NetBox (PROD.md)")
    tunnels = [tunnel(server) for server in servers]
    with ThreadPoolExecutor(len(servers)) as pool:
        snaps = list(pool.map(snapshot, servers, tunnels))
    return [overview(per_host(lambda s: summary(s, s["resources"], s["seconds"]), snaps))]


# The firmware inventory entries a component's update touches; for BIOS and BMC, those linked to it
def entries(found, component) -> list[dict]:
    groups = inventory_groups(found)
    if component in groups:
        return [d for _, d in groups[component]]
    owners = {"System BIOS": {link(d, "Bios") for _, d in of(found, "ComputerSystem")},
              "Manager (BMC)": {u for u, _ in of(found, "Manager")}}.get(component, set())
    images = [i for _, m in of(found, "Manager") for i in obj(m.get("Links")).get("SoftwareImages") or []]
    listed = set(map(uri_of, images)) if component == "Manager (BMC)" else set()
    inventory = of(found, "SoftwareInventory")
    linked = [u for u, d in inventory if u in listed or owners & set(map(uri_of, d.get("RelatedItem") or []))]
    ids = {d.get("SoftwareId") for u, d in inventory if u in linked} - {None}
    return [d for u, d in inventory if u in linked or d.get("SoftwareId") in ids]


# A SimpleUpdate parameter's AllowableValues, from the action or its ActionInfo; None when unlisted
def allowable(found, action, name) -> list | None:
    info = found.get(norm(action.get("@Redfish.ActionInfo") or "")) or {}
    listed = [p.get("AllowableValues") for p in info.get("Parameters") or [] if p.get("Name") == name]
    return action.get(f"{name}@Redfish.AllowableValues") or next(filter(None, listed), None)


# How a component's image can reach the BMC: (push, pull), each 'yes · …', 'no: why' or 'not verified: …'
def ways_in(found, component, url=None) -> tuple[str, str]:
    update = first_of(found, "UpdateService")
    targets = update_targets_of(found, component)
    if not update or update.get("ServiceEnabled") is False:
        return "no: no update service", "no: no update service"
    push = (f"yes · {update['MultipartHttpPushUri']}" if update.get("MultipartHttpPushUri")
            else "no: no MultipartHttpPushUri")
    action = obj(obj(update.get("Actions")).get("#UpdateService.SimpleUpdate"))
    if not action:
        return push, "no: no SimpleUpdate action"
    allowed_targets, protocols = allowable(found, action, "Targets"), allowable(found, action, "TransferProtocol")
    outside = [t for t in targets if allowed_targets is not None and t not in allowed_targets]
    scheme = url.split("://")[0].upper() if url and "://" in url else None
    if outside:
        return push, f"no: {', '.join(outside)} not in Targets@AllowableValues"
    if scheme and protocols is not None and scheme not in protocols:
        return push, f"no: {scheme} not in TransferProtocol@AllowableValues ({', '.join(protocols)})"
    listed = (f"{', '.join(protocols)}" if protocols is not None else "protocols not listed") + \
        (" · targets listed" if allowed_targets is not None else " · targets not listed")
    return push, ("yes · " if allowed_targets is not None and protocols is not None else "not verified: ") + listed


# What an update can target on this BMC: System BIOS, Manager (BMC) and every firmware inventory row
def update_targets(found) -> set[str]:
    return {"System BIOS", "Manager (BMC)", *inventory_groups(found)}


# Whether Redfish can update a component on this host, and why not: the Updateable column
def updatable(found, component) -> str:
    if component not in update_targets(found):
        return "no: not in the firmware inventory"
    touched = entries(found, component)
    flags = [d.get("Updateable") for d in touched]
    if not touched:
        return "not verified: no inventory entry linked"
    if any(d.get("WriteProtected") is True for d in touched):
        return "no: WriteProtected (cannot be overwritten)"
    if True in flags:
        return "yes"
    if all(flag is False for flag in flags):
        return "no: Updateable false (reporting only)"
    return "not verified: Updateable not reported"


# The targets of an update: the component's RelatedItem, else its entries; [] lets the BMC decide
def update_targets_of(found, component) -> list[str]:
    touched = entries(found, component)
    related = {u for d in touched for u in map(uri_of, d.get("RelatedItem") or []) if u}
    return sorted(related or {u for u in map(uri_of, touched) if u})


# A threshold property's DMTF Sensor name (UpperThresholdNonCritical -> UpperCaution); else None
def threshold(prop) -> str | None:
    m = THRESHOLD.search(prop)
    if not m:
        return None
    side, level = (m[1], m[2]) if m[1] else (m[3], m[4])
    return side + ("Caution" if level == "NonCritical" else level)


# One host's crawl as Prometheus samples: identity, readings, thresholds, health, firmware, BMC resources
def exposition(server, found, seconds) -> list[str]:
    host = server["host"]
    up = bool(found) and "error" not in found.get(ROOT, {})
    manufacturer, model = identity(found) if up else ("-", "-")
    out = [sample("redfish_host_info", 1, host=host, vendor=server.get("vendor", "-"), project=server.get("project", "-"),
                  rack=server.get("rack", "-"), manufacturer=manufacturer, model=model),
           sample("redfish_up", int(up), host=host), sample("redfish_crawl_seconds", seconds, host=host),
           sample("redfish_resources", len(found), host=host),
           sample("redfish_failed_requests", sum("error" in d for d in found.values()), host=host)]
    if not up:
        return out
    records = readings(found)
    limits = {}
    for r in records:
        name = r["role"] == "limit" and threshold(r["property"])
        if name and isinstance(r["value"], (int, float)) and r["value"]:
            limits.setdefault((r["resource"], r["kind"]), {})[name] = r["value"]
    for r in records:
        if r["role"] == "reading" and isinstance(r["value"], (int, float)):
            labels = {"host": host, "kind": r["kind"], "resource": r["resource"], "sensor": r["name"] or "-",
                      "property": r["property"], "unit": r["unit"] or "-"}
            out.append(sample("redfish_reading", r["value"], **labels))
            th, v = limits.get((r["resource"], r["kind"]), {}), r["value"]
            out += [sample("redfish_reading_threshold", t, **labels, threshold=name) for name, t in th.items()]
            past = lambda level: v >= th.get("Upper" + level, inf) or v <= th.get("Lower" + level, -inf)
            if th:
                out.append(sample("redfish_reading_crossed", next(
                    (n for n, level in ((3, "Fatal"), (2, "Critical"), (1, "Caution")) if past(level)), 0), **labels))
    for resource, kind, name, health, health_rollup, state in components(found):
        out.append(sample("redfish_health", {"OK": 0, "Warning": 1, "Critical": 2}.get(health, -1), host=host,
                          resource=resource, type=kind, name=name, health=health, rollup=health_rollup, state=state))
    for uri, d in found.items():
        kind = rtype(d)[0]
        if kind == "EthernetInterface" or kind == "Port" and NETWORK_PORT.search(uri):
            macs = [d.get("MACAddress"), *(obj(d.get("Ethernet")).get("AssociatedMACAddresses") or [])]
            labels = {"host": host, "resource": uri, "type": kind, "name": text(named(d)),
                      "mac": text(next(filter(None, macs), None)), "enabled": text(d.get("InterfaceEnabled")),
                      "ipv4": text(obj(next(iter(d.get("IPv4Addresses") or []), None)).get("Address"))}
            out.append(sample("redfish_network_link", LINK.get(d.get("LinkStatus"), -1), **labels))
            speed, scale = (d.get("SpeedMbps"), 1) if kind == "EthernetInterface" else (d.get("CurrentSpeedGbps"), 1000)
            if isinstance(speed, (int, float)):
                out.append(sample("redfish_network_speed_mbps", speed * scale, **labels))
    ids, targets = software_ids(found), update_targets(found)
    for component, (version, _) in firmware(found).items():
        push, pull = ways_in(found, component) if component in targets else ("-", "-")
        out.append(sample("redfish_firmware_info", 1, host=host, component=component, version=text(version),
                          software_id=ids.get(component, "-"), updateable=updatable(found, component), push=push,
                          pull=pull))
    for uri, d in of(found, "Manager"):
        if d.get("LastResetTime"):
            reset = datetime.fromisoformat(d["LastResetTime"]).timestamp()
            out.append(sample("redfish_manager_last_reset_timestamp_seconds", reset, host=host, manager=uri))
    for uri, d in of(found, "ManagerDiagnosticData"):
        memory = obj(d.get("MemoryStatistics"))
        out += [sample("redfish_bmc_memory_bytes", memory[k], host=host, type=k.removesuffix("Bytes").lower())
                for k in ("TotalBytes", "AvailableBytes") if isinstance(memory.get(k), (int, float))]
        if isinstance(d.get("FreeStorageSpaceKiB"), (int, float)):
            out.append(sample("redfish_bmc_free_storage_bytes", d["FreeStorageSpaceKiB"] * 1024, host=host))
    return out


# Crawl every BMC each interval, one session kept per BMC, and serve the latest metrics on /metrics
def exporter(args) -> list:
    servers = load_servers()
    if not servers:
        log.warning("exporter: no BMC in NetBox site %s, nothing to read", SITE.name)
    for server in servers:
        tunnel(server)
    parts = {server["host"]: [] for server in servers}
    sessions = {}
    refused = set()
    stopping = threading.Event()

    # One host's samples; a 401 on a kept session logs in again, on a fresh login the host isn't asked again
    def read(server) -> list[str]:
        host = server["host"]
        if host in refused:
            return [sample("redfish_up", 0, host=host), sample("redfish_login_refused", 1, host=host)]
        try:
            for fresh in (host not in sessions, True):
                if fresh:
                    bmc = tunnel(server)
                    sessions[host] = (bmc, login(bmc))
                found, seconds, stopped = crawl(server, sessions[host][0], args.interval * 3, keep=True)
                if stopped != REFUSED or stopping.is_set():
                    break
                sessions.pop(host)
                if fresh:
                    refused.add(host)
                    log.error("%s: login refused, not asked again until a restart: check its credentials", host)
                    break
                log.info("%s: the BMC ended its session, logging in again", host)
            timeout = first_of(found, "SessionService").get("SessionTimeout")
            if host in sessions and (not sessions[host][1] or isinstance(timeout, int) and timeout < args.interval):
                bmc, location = sessions.pop(host)
                if location:
                    logout(bmc, location)
            return exposition(server, found, seconds)
        except Exception:
            log.exception("%s: left out of this read", host)
            return [sample("redfish_up", 0, host=host)]

    # Read one BMC every interval on its own thread, so a slow one never holds up the others
    def watch(server) -> None:
        while not stopping.is_set():
            start = time.monotonic()
            parts[server["host"]] = read(server)
            time.sleep(max(0.0, args.interval - (time.monotonic() - start)))

    class Metrics(BaseHTTPRequestHandler):
        # Serve the latest samples of every host on /metrics, 404 elsewhere
        def do_GET(self) -> None:
            body = "".join(x + "\n" for part in list(parts.values()) for x in part).encode()
            self.send_response(200 if self.path == "/metrics" else 404)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body if self.path == "/metrics" else b"")

        # No access log: a scrape every interval isn't news
        def log_message(self, *args) -> None:
            pass

    for server in servers:
        threading.Thread(target=watch, args=(server,), daemon=True).start()
    address, port = args.listen.rsplit(":", 1)
    log.info("exporter: %d BMCs every %ss, metrics on http://%s/metrics", len(servers), args.interval, args.listen)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        ThreadingHTTPServer((address, int(port)), Metrics).serve_forever()
    finally:
        stopping.set()
        log.info("stopping: logging out of %d BMCs; a read still running ends on a 401, not a credentials problem",
                 len(sessions))
        for bmc, location in list(sessions.values()):
            if location:
                logout(bmc, location)
    return []


# View health: counts per host, then what is not OK, jobs to check and failed requests
def view_health(args) -> list:
    snaps = load()
    comps = dict(per_host(lambda s: (s["host"], [(s["host"], *c) for c in components(s["resources"])]), snaps))
    values = sorted({c[4] for cs in comps.values() for c in cs}, key=lambda v: (-SEVERITY.get(v, -1), v))
    jobs = dict(per_host(lambda s: (s["host"], [(s["host"], *j) for j in open_jobs(s["resources"])]), snaps))
    hosts = per_host(lambda s: (*label(s), connection(s), *identity(s["resources"]), rollup(s["resources"]),
                                *(str(Counter(c[4] for c in comps.get(s["host"], []))[v]) for v in values),
                                str(len(jobs.get(s["host"], []))), str(len(of(s["resources"], "(untyped)"))),
                                s["collected"]), snaps)
    problems = sorted((c for cs in comps.values() for c in filter(lambda c: c[4] in SEVERITY, cs)),
                      key=lambda c: (SEVERITY[c[4]], c))
    failed = [(s["host"], u, d.get("error", "-")) for s in snaps for u, d in of(s["resources"], "(untyped)")]
    failed += [(s["host"], "(crawl stopped)", s["stopped"]) for s in filter(lambda s: s.get("stopped"), snaps)]
    return [
        block("Health by host", ("Host", "Vendor", "Project", "Connection", "Manufacturer", "Model", "HealthRollup",
                                 *(f"Health {v}" for v in values), "Jobs to check", "Failed requests",
                                 "Collected"), hosts),
        block("Not OK", ("Host", "Resource", "Type", "Name", "Health", "HealthRollup", "State"), problems)
        or Text("Not OK: none"),
        block("Jobs to check", JOBS_ROW, [j for js in jobs.values() for j in js]) or Text("Jobs to check: none"),
        block("Failed requests", ("Host", "Resource", "Error"), failed) or Text("Failed requests: none")]


# Jobs and tasks not Completed, or with a Warning or Critical status or message, worst message shown
def open_jobs(found) -> list[tuple[str, ...]]:
    rows = []
    for u, d in sorted(found.items()):
        if rtype(d)[0] not in JOB_TYPES:
            continue
        state, status = d.get("JobState") or d.get("TaskState"), d.get("JobStatus") or d.get("TaskStatus")
        messages = [m for m in d.get("Messages") or [] if isinstance(m, dict)]
        worst = min(messages, key=lambda m: SEVERITY.get(m.get("MessageSeverity") or m.get("Severity"), 2), default={})
        severity = worst.get("MessageSeverity") or worst.get("Severity")
        if state != "Completed" or status in SEVERITY or severity in SEVERITY:
            rows.append((u, text(d.get("Name")), text(state), text(status), text(worst.get("Message")),
                         text(d.get("EndTime") or d.get("StartTime"))))
    return rows


# {component: (version, note)} for one host: BIOS, BMC, firmware inventory, drives, NICs, PSUs...
def firmware(found) -> dict[str, tuple[str | None, str]]:
    parts = {}
    for prefix, source, model, version in FW_PARTS:
        for _, item in items(found, source):
            flat = properties(item)
            for v in filter(real, (v for p, v in filter(lambda pv: re.fullmatch(version, pv[0]), flat.items()))):
                parts.setdefault(f"{prefix} {text(flat.get(choose(flat, model)))}", set()).add(text(v))
    return {"System BIOS": (first_of(found, "ComputerSystem").get("BiosVersion"), ""),
            "Manager (BMC)": (first_of(found, "Manager").get("FirmwareVersion"), ""),
            **images(found),
            **{part: (" ".join(sorted(versions)), "") for part, versions in parts.items()}}


# {name: (running version, note)} per firmware component
def images(found) -> dict[str, tuple[str | None, str]]:
    return dict(component(group) for group in inventory_groups(found).values())


# {name: [(linked as active, entry)]} per firmware component, grouped by SoftwareId, else Name
def inventory_groups(found) -> dict[str, list[tuple[bool, dict]]]:
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


# A model's baseline for one host: a SoftwareId:<id> line applies to the row carrying that id
def host_baseline(baseline, ids) -> dict[str, str]:
    out = {k: v for k, v in baseline.items() if not k.startswith("SoftwareId:")}
    matched = set()
    for name, sid in ids.items():
        key = f"SoftwareId:{sid}"
        if key in baseline:
            out.setdefault(name, baseline[key])
            matched.add(key)
    return {**out, **{k: v for k, v in baseline.items() if k.startswith("SoftwareId:") and k not in matched}}


# One component's (name, (running version, note)) from its (linked as active, entry) images
def component(group) -> tuple[str, tuple[str | None, str]]:
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


# The state of an image that isn't running: ImageState, Staged, Armed, Inactive when Active is false
def image_state(d) -> str | None:
    if d.get("ImageState"):
        return d["ImageState"]
    if d.get("Staged") or d.get("Armed"):
        return "Staged" if d.get("Staged") else "Armed"
    return "Inactive" if d.get("Active") is False else None


# Components x hosts of one model, checked against the model's baseline, or else against each other
def matrix(title, rows) -> Table:
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


# One host's version: green on baseline, red off it, yellow when it differs from the most common
def version_cell(version, note, want, common) -> Text:
    if want is not None:
        style = "green" if version == want else "bold red"
    else:
        style = "bold yellow" if version != common else ""
    cell = Text(version, style=style)
    if note:
        cell.append("\n" + note, style="dim")
    return cell


# One compliance row: how many baseline components a host has at the approved version, and the rest
def compliance(host, model, fw, baseline) -> tuple[str, ...]:
    have = {c: text(fw.get(c, (None, ""))[0]) for c in baseline}
    reported = {c: have[c] if fw.get(c, (None, ""))[0] is not None else "not reported" for c in baseline}
    needs = [f"{c}: {reported[c]} → {want}" for c, want in baseline.items() if have[c] != want]
    return (*host, model, f"{len(baseline) - len(needs)}/{len(baseline)}", "\n".join(needs) or "-")


# {model: {component: approved version}} from baseline.yaml, values as text (2.20 stays 2.20)
def load_baseline(path=BASELINE) -> dict[str, dict[str, str]]:
    try:
        return yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader) or {}
    except FileNotFoundError:
        return {}


# View firmware: baseline compliance per host, then one matrix per hardware model
def view_firmware(args) -> list:
    baselines, groups = load_baseline(), {}
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


# One host of the firmware view: (its model, (its label, its firmware, its baseline))
def firmware_row(snap, baselines) -> tuple[str, tuple]:
    found = snap["resources"]
    model = " ".join(identity(found))
    return model, (label(snap), firmware(found), host_baseline(baselines.get(model) or {}, software_ids(found)))


# One CMDB row: identity, CPUs, GPUs, memory, drives, NICs and power supplies of one host
def inventory(snap) -> tuple[str, ...]:
    found = snap["resources"]
    system = properties(first_of(found, "ComputerSystem"))
    processors = [properties(d) for _, d in of(found, "Processor")]
    cpus = list(filter(lambda p: p.get("ProcessorType") in (None, "CPU"), processors))
    gpus = list(filter(lambda p: p.get("ProcessorType") == "GPU", processors))
    cpu = next(filter(real, [system.get("ProcessorSummary/Model"),
                             *(p.get(choose(p, "Model|ProcessorId/EffectiveFamily")) for p in cpus)]), None)
    dimms = [d.get("CapacityMiB") or 0 for _, d in of(found, "Memory")]
    sizes = (d.get("CapacityBytes") or 0 for _, d in of(found, "Drive"))
    disks = list(filter(lambda b: b >= 1e9, sizes))
    psus = [i for _, i in items(found, "PowerSupply") + items(found, "Power/PowerSupplies")]
    host_nics = filter(lambda u: u.startswith(ROOT + "/Systems/"), dict(of(found, "EthernetInterface")))
    present = list(filter(lambda i: obj(i.get("Status")).get("State") != "Absent", psus))
    return (*label(snap), *identity(found), first_real(found, "SerialNumber"), text(system.get("UUID")),
            f"{system.get('ProcessorSummary/Count') or len(cpus)} × {text(cpu)}",
            text(sum(p.get("TotalCores") or 0 for p in cpus) or None), str(len(gpus)),
            text(system.get("MemorySummary/TotalSystemMemoryGiB")),
            f"{sum(1 for d in dimms if d)}/{len(dimms)}" if dimms else "-",
            f"{len(disks)} · {sum(disks) / 1e12:.1f} TB" if disks else "-",
            str(len(of(found, "NetworkAdapter")) or len(list(host_nics))),
            f"{len(present)}/{len(psus)}")


# View inventory: one row per reachable host
def view_inventory(args) -> list:
    snaps, hosts = reachable(), [s["host"] for s in load()]
    missing = ", ".join(sorted(set(hosts) - {s["host"] for s in snaps})) or "none"
    return [block("Inventory", INVENTORY_ROW, per_host(inventory, snaps), caption_justify="left",
                  caption=f"{len(snaps)} of {len(hosts)} hosts · not (fully) collected: {missing}")]


# Telemetry per host: readings count, hottest temperature against its critical threshold, highest power
def hotspot(snap, rs) -> tuple[str, ...]:
    rs = list(filter(lambda r: r["role"] == "reading", rs))
    numeric = lambda kind: filter(lambda r: r["kind"] == kind and type(r["value"]) in (int, float), rs)
    hot = max(numeric("Temperature"), key=lambda r: r["value"], default={})
    power = max((r["value"] for r in numeric("Power")), default=None)
    not_ok = list(filter(lambda r: r["health"] in SEVERITY, rs))
    return (*label(snap), str(len(rs)), text(hot.get("name")), text(hot.get("value")), text(hot.get("critical")),
            text(margin(hot)), text(power), str(len(not_ok)))


# One row per sensor reading or limit; Critical and Margin are '-' without a threshold
def sensor_row(r) -> tuple[str, ...]:
    headroom = margin(r) if r["role"] == "reading" else None
    return (text(r["kind"]), r["resource"], text(r["name"]), r["property"], r["role"], text(r["value"]),
            text(r["unit"]), text(r["critical"]), text(headroom), text(r["health"]), text(r["state"]), r["source"])


# View telemetry: per host the hottest temperature and highest power, then every sensor
def view_telemetry(args) -> list:
    host_readings = per_host(lambda s: (s, readings(s["resources"])), reachable())
    order = lambda r: (text(r["kind"]), r["resource"], r["role"] != "reading", r["property"])
    return [block("Telemetry by host", TELEMETRY_ROW, [hotspot(s, rs) for s, rs in host_readings]),
            *filter(None, (block(f"Sensors · {' · '.join(label(s))}", SENSORS,
                                 list(map(sensor_row, sorted(rs, key=order)))) for s, rs in host_readings))]


# What a hardware model can do, from one of its hosts
def capabilities(model, snaps) -> tuple[str, ...]:
    snap = snaps[0]
    found = snap["resources"]
    types = sorted({rtype(d)[0] for d in found.values()})
    offered = ", ".join(t.removesuffix("Service") for t in filter(lambda t: t.endswith("Service"), types))
    acts = action_list(found)
    simple = map(lambda k: parameters(found, acts[k]), filter(lambda k: k[1] == "#UpdateService.SimpleUpdate", acts))
    update = first_of(found, "UpdateService")
    telemetry = (f"{len(of(found, 'MetricDefinition'))} metric, {len(of(found, 'MetricReportDefinition'))} report "
                 f"definitions · {len(of(found, 'Triggers'))} triggers")
    return (" ".join(model), str(len(snaps)), snap["host"], text(found.get(ROOT, {}).get("RedfishVersion")),
            str(len(types)), offered or "-", str(len(acts)), next(simple, "-"),
            ", ".join(filter(update.__contains__, ("HttpPushUri", "MultipartHttpPushUri"))) or "-",
            telemetry if first_of(found, "TelemetryService") else "-", str(len(of(found, "Sensor"))))


# View capabilities: one row per hardware model
def view_capabilities(args) -> list:
    groups = {}
    for snap in reachable():
        groups.setdefault(identity(snap["resources"]), []).append(snap)
    return [block("Capabilities by model", CAPABILITIES_ROW,
                  [capabilities(model, snaps) for model, snaps in sorted(groups.items())])]


# {(resource, property): value} without annotations, numbers, clocks and metric reports
def stable(found) -> dict[tuple[str, str], Any]:
    keep = lambda pv: "@" not in pv[0] and type(pv[1]) not in (int, float) and not VOLATILE.search(pv[0])
    docs = filter(lambda ud: rtype(ud[1])[0] != "MetricReport", found.items())
    return {(u, p): v for u, d in docs for p, v in filter(keep, properties(d).items())}


# (resource, property, before, after) between two crawls: resources added or removed, then properties
def changes(old, new) -> list[tuple[str, str, str, str]]:
    both = new.keys() & old.keys()
    before, after = (stable({u: f[u] for u in both}) for f in (old, new))
    changed = filter(lambda k: before.get(k) != after.get(k), sorted(before.keys() | after.keys()))
    return ([(u, "(resource)", "-", "added") for u in sorted(new.keys() - old.keys())]
            + [(u, "(resource)", "present", "removed") for u in sorted(old.keys() - new.keys())]
            + [(u, p, text(before.get((u, p))), text(after.get((u, p)))) for u, p in changed])


# View diff: what changed on each host since the previous collect
def view_diff(args) -> list:
    pairs = per_host(lambda s: (s, previous(s["host"])), load())
    compared = list(filter(lambda sp: sp[1], pairs))
    rows = [(s["host"], *c) for s, old in compared for c in changes(old["resources"], s["resources"])]
    since = f"{len(compared)} of {len(pairs)} hosts compared with their previous collect"
    return [block("Changes", ("Host", "Resource", "Property", "Before", "After"), rows, caption=since,
                  caption_justify="left") or Text(f"No changes · {since}")]


# View detail: the full report of one host
def view_detail(args) -> list:
    try:
        snap = json.loads(snapshot_path(args.host).read_text())
    except FileNotFoundError:
        sys.exit(f"no snapshot for {args.host}: run 'poller.py collect' first")
    return [report(snap, snap["resources"])]


# Parse the command, run it, print what it returns and write the files asked for
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    output = output_options()
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    command = lambda name, run, help: commands.add_parser(name, parents=[output], help=help).set_defaults(run=run)
    command("collect", collect, "crawl every BMC of the site, snapshot each host")
    command("health", view_health, "what is broken right now: health, jobs to check, failed requests")
    command("firmware", view_firmware, "firmware per hardware model, against the baseline")
    command("inventory", view_inventory, "one row per host: what we have")
    command("telemetry", view_telemetry, "every sensor of every host, with its limits")
    command("capabilities", view_capabilities, "what each hardware model can do")
    command("diff", view_diff, "what changed since the previous collect")
    e = commands.add_parser("exporter", help="read every BMC each interval, serve the metrics to Prometheus")
    e.add_argument("--listen", default="127.0.0.1:9101", help="address:port to serve /metrics on [127.0.0.1:9101]")
    e.add_argument("--interval", type=float, default=10, help="seconds between two reads of every BMC [10]")
    e.set_defaults(run=exporter)
    d = commands.add_parser("detail", parents=[output], help="everything about one host")
    d.add_argument("host")
    d.set_defaults(run=view_detail)
    args = p.parse_args()
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    show(args.run(args), args)


if __name__ == "__main__":
    main()
