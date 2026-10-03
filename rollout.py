#!/usr/bin/env python3
"""Firmware rollout, in six steps: 1 plan and pre-flight (canary and waves), 2 drain when the reset touches the host,
3 update, 4 reset what activates the new firmware, 5 post-check, 6 undrain, or roll back and then undrain.

  plan              One report per host, the hosts grouped by vendor (--hosts for some of them): the host's update
                    readiness, then every firmware component in rollout order (BMC, BIOS, the rest) with its
                    SoftwareId, Updateable, A/B bank, Rollback and, for a component the baseline lists, its
                    pre-flight verdict.
  plan COMPONENT    COMPONENT is a row name of the firmware view and of the baseline, e.g. "Manager (BMC)", or
                    SoftwareId:<id> for the row whose entries carry that id on each host.
                    Per host a verdict with every reason: go, skip (nothing to do now, or busy: try a later wave) or
                    block (a person has to look). The hosts that pass go into waves: a canary, then waves spread
                    across racks (the inventory's optional rack, else its project). One table per vendor.

Pre-flight never updates without a way back and never goes down, or in an unknown direction, by accident: it needs
the target version's image in the image catalog (file and sha256), and blocks a downgrade or an order it can't tell
unless --allow-downgrade, and a component without a rollback path unless --accept-no-rollback.

  report            The latest pipeline's report, or --plan FILE [RECORD…]: the verdict and, when it halted, why;
                    what needs a person (host, why, next step); the versions, a letter each; every host by wave with
                    the wave's gate; the hosts the plan left out. --details adds every host's steps with their
                    evidence, --host one host's; --save FILE writes it all as JSON (rollout-report/1) for services.

In a pipeline: plan --save plan.json, then run --plan plan.json --stage canary, then --stage waves. The saved plan
decides the component, files, options and waves; each stage exits 1 when its gate fails, so the next one doesn't run.
  run COMPONENT     Steps 2 to 6 on the hosts that pass, wave by wave; each host is read and pre-flighted again right
                    before it is touched. Without --yes a dry run: what each host would get. With --yes only hosts
                    marked writable: true in the inventory are updated (the lab); every step is recorded, its intent
                    before it and its result after, in <site>/runs/<run id>.jsonl. A failing canary, or a failure rate
                    over --halt-at, stops the run before the next wave.

The site is the fleet: SITE=prod (the default) or SITE=lab, a folder with the inventory (inventory.yaml), the
baseline (baseline.yaml), the image catalog (images.yaml, its files relative to the folder) and the run records."""
import argparse
import hashlib
import json
import logging
import math
import os
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path

import requests
import urllib3
import yaml
from rich.rule import Rule
from rich.table import Column, Table
from rich.text import Text

from constants import (ACTION_ROW, ACTIONS_ROW, APPLY_TIME, BASELINE, CACHE_CA, DOTTED, FAILED_STATES, FAULTS,
                       HOST_ROW, IMAGE_CACHE, IMAGES, INVENTORY_FILE, LEFT_OUT_ROW, NEXT_STEP, PLAN_ROW, PREFLIGHT_ROW,
                       PROPS, REPORT_SCHEMA, RESET_PREFERENCE, ROLLOUT, ROLLOUT_DEFAULTS, ROOT, RUNNING_STATES, RUNS,
                       SITE, SPOOL, STEP_ROW, STYLE, TASK_TIMEOUT, VERSIONS_ROW, WAVE_ROW)
from helpers import (block, first_of, first_real, identity, label, link, norm, obj, of, output_options, reason, sample,
                     show, text, uri_of)
from poller import (allowable, entries, firmware, host_baseline, image_state, load_baseline, load_servers, open_jobs,
                    software_ids, update_targets, update_targets_of, updatable, ways_in)
from redfish import call, login, logout, send, tunnel

log = logging.getLogger("redfish")


# ---- Live read: what pre-flight needs from one BMC, about 10-30 GETs instead of the poller's full crawl ----

# GET a resource into found; for a collection also every member, following Members@odata.nextLink pages
def grab(tunnel, uri, found) -> dict:
    doc = page = found[uri] = call(tunnel, uri)
    while True:
        for member in page.get("Members") or []:
            member_uri = uri_of(member)
            if member_uri:
                found[member_uri] = call(tunnel, member_uri)
        next_page = page.get("Members@odata.nextLink")
        if not next_page or next_page in found:
            return doc
        page = found[next_page] = call(tunnel, next_page)


# The resources pre-flight reads, as {uri: document}, like a snapshot's resources but fresh
def live_read(tunnel) -> dict[str, dict]:
    """The service root; systems, chassis and managers; the update service with its firmware inventory and the
    SimpleUpdate ActionInfo; every task and job; the reset ActionInfo of each manager and system, which lists the
    reset types. ServiceRoot links the TaskService as "Tasks"."""
    found = {}
    root = grab(tunnel, ROOT, found)
    for key in ("Systems", "Chassis", "Managers", "UpdateService", "Tasks", "JobService"):
        if link(root, key):
            grab(tunnel, link(root, key), found)
    services = ((link(root, "UpdateService"), "FirmwareInventory"), (link(root, "Tasks"), "Tasks"),
                (link(root, "JobService"), "Jobs"))
    for service, key in services:
        uri = link(found.get(service) or {}, key)
        if uri:
            grab(tunnel, uri, found)
    update = found.get(link(root, "UpdateService")) or {}
    info = obj(obj(update.get("Actions")).get("#UpdateService.SimpleUpdate")).get("@Redfish.ActionInfo")
    if info:
        grab(tunnel, norm(info), found)
    for kind, action in (("Manager", "#Manager.Reset"), ("ComputerSystem", "#ComputerSystem.Reset")):
        for _, d in of(found, kind):
            info = obj(obj(d.get("Actions")).get(action)).get("@Redfish.ActionInfo")
            if info:
                grab(tunnel, norm(info), found)
    return found


# Log in, read, log out: (the read, when it was taken); a BMC that stopped answering reads as {ROOT: {"error": why}}
def read_host(server, tunnel) -> tuple[dict, float]:
    tunnel[2]["until"] = time.monotonic() + tunnel[2]["deadline"]
    try:
        location = login(tunnel)
        try:
            found = live_read(tunnel)
        finally:
            if location:
                logout(tunnel, location)
    except ConnectionAbortedError as e:  # raised by call(): 401, the deadline, or no answer max_failures times
        found = {ROOT: {"error": str(e)}}
    return found, time.time()


# ---- Pre-flight: one host, one component ----

# A version as something sortable: dot-separated integers, 2.86.86.86, under DotIntegerNotation or no VersionScheme.
# None otherwise: an order nobody can tell, as for SemVer or OEM versions, which no BMC of the fleet reports
def version_key(version, scheme) -> tuple | None:
    if scheme in (None, "DotIntegerNotation") and DOTTED.fullmatch(version):
        return tuple(map(int, version.split(".")))
    return None


# Which way going from the running version to want is, by the VersionScheme the component's entries report:
# (1 up, -1 down, 0 the same, None unknown; how it was told)
def direction(found, component, running, want) -> tuple[int | None, str]:
    scheme = next(filter(None, (d.get("VersionScheme") for d in entries(found, component))), None)
    a, b = version_key(running, scheme), version_key(want, scheme)
    if a is None or b is None:
        return None, "unknown"
    return (b > a) - (b < a), scheme or "dotted integers"


def update_methods(found, update) -> list[str]:
    action = obj(obj(update.get("Actions")).get("#UpdateService.SimpleUpdate"))
    protocols = allowable(found, action, "TransferProtocol")
    return [*(["multipart"] if update.get("MultipartHttpPushUri") else []),
            *([f"SimpleUpdate ({', '.join(protocols or ['protocols not listed'])})"] if action else []),
            *(["HttpPushUri"] if update.get("HttpPushUri") else [])]


# Whether a component has a second firmware image, an A/B bank, as far as standard Redfish reports it
def ab_bank(found, component) -> str:
    """Yes: besides the running image (Active true, ImageState Active, or a manager's ActiveSoftwareImage), another
    image of the component is reported, inactive, staged or armed; a manager's SoftwareImages count as its images.
    Not reported: one image, several without saying which runs, or none linked. One reported image doesn't prove one
    bank: a BMC may keep a second bank it doesn't expose (AMI shows it only in its Oem, see make detail SITE=prod)."""
    if component not in update_targets(found):
        return "-"
    touched = entries(found, component)
    running = marked_running(found, touched)
    others = [d for d in touched if not any(d is r for r in running)]
    if running and others:
        return "yes: " + ", ".join(f"{image_state(d) or 'Inactive'} {text(d.get('Version'))}" for d in others)
    if not touched:
        return "not reported: no image linked"
    if len(touched) == 1:
        return "not reported: one image"
    return f"not reported: {len(touched)} images, none marked Active"


# The images among touched that run now: Active true, ImageState Active, or a manager's ActiveSoftwareImage
def marked_running(found, touched) -> list[dict]:
    active = {uri_of(obj(m.get("Links")).get("ActiveSoftwareImage")) for _, m in of(found, "Manager")} - {None}
    return [d for d in touched if d.get("Active") is True or d.get("ImageState") == "Active" or uri_of(d) in active]


# The row name a component key means on this host: SoftwareId:<id> becomes the row whose entries carry that id
def resolve(found, key) -> str:
    ids = {f"SoftwareId:{sid}": name for name, sid in software_ids(found).items()}
    return ids.get(key, key)


# ---- Images: the files an update installs and a rollback reinstalls, kept in images.yaml ----

# {model: {component: {version: {"file", "sha256"}}}} from images.yaml, all text; {} without it
def load_images(path=IMAGES) -> dict:
    """A component is a row name or SoftwareId:<id>, as in the baseline. file is a path relative to the site folder
    for a multipart push, or a URL a BMC pulls with SimpleUpdate."""
    try:
        return yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader) or {}
    except FileNotFoundError:
        return {}


# The images.yaml entry for a version of a component on a model: under its row name, else its SoftwareId; None
def image_for(images, found, model, component, version) -> dict | None:
    catalog = images.get(model) or {}
    sid = software_ids(found).get(component)
    return (catalog.get(component) or catalog.get(f"SoftwareId:{sid}") or {}).get(version)


# Where the agent keeps an image to check and push: its copy from the site's cache, else the file next to images.yaml;
# None for a URL, which the BMC pulls
def image_path(file) -> Path | None:
    return None if "://" in file else (SPOOL if IMAGE_CACHE else IMAGES.parent) / file


# Download an image from the site's cache to where image_path keeps it; None, or why it couldn't
def fetch(file, path) -> str | None:
    url = f"{IMAGE_CACHE.rstrip('/')}/{file}"
    part = path.with_name(path.name + ".part")  # renamed only once complete, so a broken download is never used
    path.parent.mkdir(parents=True, exist_ok=True)
    verify = str(CACHE_CA) if CACHE_CA.is_file() else True
    try:
        with requests.get(url, stream=True, timeout=(5, 300), verify=verify) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    except (requests.RequestException, OSError) as e:
        part.unlink(missing_ok=True)
        return f"not fetched from the image cache: {reason(str(e))}"
    part.replace(path)
    return None


# What is wrong with an image: no such file, no sha256, a sha256 that doesn't match; () when nothing
@lru_cache(maxsize=None)
def image_problems(file, sha256) -> tuple[str, ...]:
    """A URL can't be checked from here: the BMC pulls it when the update runs, and that step verifies it. With a site
    cache, the image is fetched first, once; a copy whose sha256 doesn't match is deleted, so the next run fetches it
    again."""
    path = image_path(file)
    if path is None:
        return ()
    if IMAGE_CACHE and not path.is_file() and (error := fetch(file, path)):
        return (f"{file}: {error}",)
    if not path.is_file():
        return (f"{file}: no such file",)
    if not sha256:
        return (f"{file}: no sha256 in {IMAGES.name}",)
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        # lambda: the next MiB of the file, b"" at its end
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() == sha256.lower():
        return ()
    if IMAGE_CACHE:
        path.unlink()
    return (f"{file}: sha256 doesn't match {IMAGES.name}",)


# How a component could go back to the version it runs now, after an update: the Rollback column
def rollback_path(found, component, images, model, running) -> str:
    """bank: another image of it is reported (A/B) and the standard gives a way to switch to it: the manager's
    ActiveSoftwareImage for Manager (BMC), an Activate action on its entries or on the update service. reinstall:
    images.yaml has a usable image of the running version. none: neither."""
    touched = entries(found, component)
    switch = (component == "Manager (BMC)" and any(obj(m.get("Links")).get("SoftwareImages")
                                                   for _, m in of(found, "Manager"))
              or any("#SoftwareInventory.Activate" in obj(d.get("Actions")) for d in touched)
              or "#UpdateService.Activate" in obj(first_of(found, "UpdateService").get("Actions")))
    if ab_bank(found, component).startswith("yes") and switch:
        return "bank: switch back to the other image"
    image = image_for(images, found, model, component, text(running))
    if image and not image_problems(image.get("file", ""), image.get("sha256", "")):
        return f"reinstall {text(running)} from {image.get('file')}"
    return "none"


# The reset types an action accepts, from its AllowableValues or its ActionInfo; None when neither lists them
def allowed_resets(found, doc, action) -> list[str] | None:
    act = obj(obj(doc.get("Actions")).get(action))
    info = found.get(norm(act.get("@Redfish.ActionInfo") or "")) or {}
    listed = [p.get("AllowableValues") for p in info.get("Parameters") or [] if p.get("Name") == "ResetType"]
    return act.get("ResetType@Redfish.AllowableValues") or next(iter(listed), None)


# The reset types an action accepts and its apply times, as text for the readiness table
def reset_types(found, doc, action) -> str:
    act = obj(obj(doc.get("Actions")).get(action))
    if not act:
        return "-"
    times = obj(act.get("@Redfish.OperationApplyTimeSupport")).get("SupportedValues")
    types = allowed_resets(found, doc, action)
    return ", ".join(types or ["types not listed"]) + (f" · apply: {', '.join(times)}" if times else "")


# Who a host is beyond its address: the model, the Redfish service's UUID, and a serial number where one is reported
def identity_of(found) -> dict:
    serial = first_real(found, "SerialNumber")
    return {"model": " ".join(identity(found)), "service_uuid": found.get(ROOT, {}).get("UUID"),
            "serial": None if serial == "-" else serial}


# Pre-flight for one host: whether the update can be done, and every check made to decide it
def check(found, baselines, key, policy=None) -> dict:
    """key is a row name or SoftwareId:<id>. policy: allow_downgrade, accept_no_rollback, images (images.yaml).
    Returns verdict (go, skip or block), checks (every check made, in order, as {check, ok, detail}), reasons (the
    failed ones), notes, and the table's values. Nothing to do (not in the baseline, already on it) is a skip and stops
    there. Otherwise every check runs: work still running, or an image already staged, is a skip (try a later wave);
    anything that could harm the host or can't be verified is a block. A block wins over a skip."""
    policy = {"allow_downgrade": False, "accept_no_rollback": False, "images": {}, **(policy or {})}
    result = {"model": "-", "component": key, "updateable": "-", "ab": "-", "rollback": "-", "running": None,
              "want": None, "direction": "-", "methods": "-", "notes": [], "checks": []}
    blocks, skips = [], []

    # One check, recorded either way; a failed one blocks the host, or with defer=True skips it for a later wave
    def test(name, ok, detail, defer=False) -> bool:
        result["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            (skips if defer else blocks).append(detail)
        return bool(ok)

    def verdict() -> dict:
        return {**result, "verdict": "block" if blocks else "skip" if skips else "go", "reasons": blocks + skips}

    error = found.get(ROOT, {}).get("error")
    if not test("live read", not error, f"no live read: {error}" if error else "the BMC answered"):
        return verdict()
    model, component = " ".join(identity(found)), resolve(found, key)
    result.update(model=model, component=component, updateable=updatable(found, component),
                  ab=ab_bank(found, component), identity=identity_of(found))
    want = result["want"] = host_baseline(baselines.get(model) or {}, software_ids(found)).get(component)
    if not test("baseline", want is not None, f"{want} approved for {model}" if want else
                f"not in the baseline for {model}", defer=True):
        return verdict()
    if not test("update target", component in update_targets(found), "in the firmware inventory" if component in
                update_targets(found) else "not in the firmware inventory, so no update target: use its inventory row"):
        return verdict()
    running, note = firmware(found)[component]
    result.update(running=running, rollback=rollback_path(found, component, policy["images"], model, running))
    if not test("running version", running is not None, f"{running}" if running else "running version not reported"):
        result["notes"].append(note)
        return verdict()
    target = image_for(policy["images"], found, model, component, want)
    order, basis = direction(found, component, text(running), want)
    if not test("needs the update", text(running) != want and order != 0,
                f"{running} → {want}" if text(running) != want else "already on the baseline version", defer=True):
        return verdict()

    allowed = policy["allow_downgrade"]
    result["direction"] = "unknown" if order is None else f"{'up' if order > 0 else 'down'} ({basis})"
    if order is None:
        test("direction", allowed, "unknown: not dotted integers, so their order can't be told; " +
             ("allowed by --allow-downgrade" if allowed else "--allow-downgrade if intended"))
    else:
        test("direction", order > 0 or allowed, f"{'up' if order > 0 else 'down'} by {basis}" + (
            "" if order > 0 else "; allowed by --allow-downgrade" if allowed else ": --allow-downgrade if intended"))
    can = result["updateable"]
    test("Updateable", not can.startswith("no:"),
         "Redfish can't update it: " + can.removeprefix("no: ") if can.startswith("no:") else can)
    if can.startswith("not verified"):
        result["notes"].append(f"Updateable {can}, the BMC decides")
    for d in entries(found, component):
        name, state, lowest = text(d.get("Id")), image_state(d), d.get("LowestSupportedVersion")
        floor, goal = version_key(text(lowest), d.get("VersionScheme")), version_key(want, d.get("VersionScheme"))
        if lowest and floor is not None and goal is not None:
            test("LowestSupportedVersion", goal >= floor, f"{name}: {want} against LowestSupportedVersion {lowest}")
        test("nothing staged", state not in ("Staged", "Armed"), f"{name}: image {state or 'state not reported'}",
             defer=True)

    update = first_of(found, "UpdateService")
    if test("image in the catalog", target is not None,
            target["file"] if target else f"no image of {want} in {IMAGES.name}"):
        file, limit = target.get("file", ""), update.get("MaxImageSizeBytes")
        problems = image_problems(file, target.get("sha256", ""))
        test("image sha256", not problems, "; ".join(problems) or f"{file}: sha256 matches the catalog")
        path = image_path(file)
        if path is None:
            result["notes"].append(f"image {file} is a URL: checked when the update pulls it")
        elif path.is_file() and isinstance(limit, int):
            test("image size", path.stat().st_size <= limit,
                 f"{path.stat().st_size} bytes, MaxImageSizeBytes {limit}")
    test("rollback path", result["rollback"] != "none" or policy["accept_no_rollback"],
         result["rollback"] if result["rollback"] != "none" else f"no rollback: no backup image reported and no image "
         f"of {text(running)} in {IMAGES.name}" + ("; accepted by --accept-no-rollback" if policy["accept_no_rollback"]
                                                    else "; --accept-no-rollback to update anyway"))

    methods = update_methods(found, update)
    result["methods"] = ", ".join(methods) or "-"
    test("update service", update and update.get("ServiceEnabled") is not False and methods,
         "no UpdateService" if not update else "UpdateService disabled" if update.get("ServiceEnabled") is False
         else f"UpdateService: {result['methods']}" if methods else "no update method")
    file = (target or {}).get("file", "")
    push, pull = ways_in(found, component, file if "://" in file else None)
    if target:
        way = pull if "://" in file else push
        test("pull allowed" if "://" in file else "push allowed", not way.startswith("no:"), way)
    if "HttpPushUriTargetsBusy" in update:
        test("push targets free", update["HttpPushUriTargetsBusy"] is not True,
             f"HttpPushUriTargetsBusy {text(update['HttpPushUriTargetsBusy'])}", defer=True)

    for kind in ("ComputerSystem", "Manager"):
        for u, d in of(found, kind):
            status = obj(d.get("Status"))
            health = status.get("HealthRollup") or status.get("Health")
            test("health", health == "OK", f"{u}: {'not reported' if health is None else health}")
    jobs = open_jobs(found)
    for u, name, state, status, message, _ in jobs:
        # still running: try again in a later wave; ended badly or needs a person: block
        why = f"{u.rsplit('/', 1)[-1]} {name}: {state}" + (f", {message}" if message != "-" else "")
        test("jobs and tasks", False, why, defer=state in RUNNING_STATES)
    if not jobs:
        test("jobs and tasks", True, "none open, none failed")
    return verdict()


# ---- Plan: First step when we want to update firmware images of resources exposed from BMC ----

# The site's rollout policy (<site>/rollout.yaml over the defaults), each value overridden by its option when given
def load_rollout(args) -> dict:
    path = ROLLOUT
    written = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
    unknown = set(written) - set(ROLLOUT_DEFAULTS)
    if unknown:
        raise SystemExit(f"{path}: unknown {', '.join(sorted(unknown))}; known: {', '.join(ROLLOUT_DEFAULTS)}")
    policy = {**ROLLOUT_DEFAULTS, **written, "file": os.path.relpath(path) if path.is_file() else None}
    for key in ("canary_per_model", "waves", "max_per_rack", "halt_at"):
        if getattr(args, key, None) is not None:
            policy[key] = getattr(args, key)
    policy["waves"] = sorted({float(w) for w in policy["waves"]} | {100.0})  # the last wave takes every host left
    if not all(0 < w <= 100 for w in policy["waves"]):
        raise SystemExit(f"waves are cumulative percentages, each over 0 and at most 100: {policy['waves']}")
    return policy


# The policy in one line, for the plan, the run and the report
def rollout_text(policy) -> str:
    cap = f"≤{policy['max_per_rack']} per rack" if policy["max_per_rack"] else "no rack limit"
    return (f"canary {policy['canary_per_model']} per model, waves to {', '.join(f'{w:g}' for w in policy['waves'])}%"
            f" of the rest, {cap}, the first {policy['strict_waves']} strict then halt over {policy['halt_at']:.0%}"
            f" failed, {policy['max_parallel'] or 'all'} at once, soak {took(policy['soak'])}")


# The plan's waves: a canary of each hardware model first, then waves that grow as the policy's cumulative percentages
# say, the hosts spread over the racks in turn and never more than max_per_rack of one rack in a wave
def waves(servers, models, policy) -> list[tuple[str, list[dict]]]:
    """servers: the hosts that pass pre-flight; models: {host: its hardware model}. Hosts marked canary: true in the
    inventory are the canary of their model first. A wave the rack limit keeps short leaves its hosts to the next."""
    # lambda s: a host -> its rack, else its project: waves spread across them
    rack = lambda s: s.get("rack") or s.get("project") or "-"
    racks = {}
    for s in servers:
        racks.setdefault(rack(s), []).append(s)
    order = [s for turn in zip_longest(*racks.values()) for s in turn if s]  # one of each rack in turn
    canary = []
    for model in dict.fromkeys(models[s["host"]] for s in order):
        # lambda s: a host of the model -> False for one marked canary: true, so those sort first
        mine = sorted((s for s in order if models[s["host"]] == model), key=lambda s: s.get("canary") is not True)
        canary += mine[:policy["canary_per_model"]]
    queue = [s for s in order if s not in canary]
    marks = [math.ceil(len(queue) * w / 100) for w in policy["waves"]]
    sizes = [b - a for a, b in zip([0, *marks], marks) if b > a] or [len(queue)]
    named, cap = [("canary", canary)], policy["max_per_rack"]
    while queue:
        size, wave, per_rack, left = sizes[min(len(named) - 1, len(sizes) - 1)], [], Counter(), []
        for s in queue:
            if len(wave) < size and (not cap or per_rack[rack(s)] < cap):
                wave.append(s)
                per_rack[rack(s)] += 1
            else:
                left.append(s)
        named.append((f"wave {len(named)}", wave))
        queue = left
    return [(name, hosts) for name, hosts in named if hosts]


# The health of a system or manager as reported: HealthRollup, else Health
def health(d) -> str:
    status = obj(d.get("Status"))
    return text(status.get("HealthRollup") or status.get("Health"))


# Whether an update's task can be followed: the TaskService, its standard policies and the tasks it holds now
def task_service(found) -> str:
    service = first_of(found, "TaskService")
    if not service:
        return "none: only an update's task monitor can say it ended"
    if service.get("ServiceEnabled") is False:
        return "disabled"
    events = {True: "yes", False: "no"}.get(service.get("LifeCycleEventOnTaskStateChange"), "not reported")
    return (f"enabled · completed tasks overwrite {text(service.get('CompletedTaskOverWritePolicy'))} · events on "
            f"state change {events} · {len(of(found, 'Task'))} tasks now")


# One host's report: its update readiness, then every firmware component with its pre-flight verdict
# Components- Resources who are exposed from the BMC cannot updated OOB with redfish service - in bound only (their tools)
def host_report(server, found, read_at, baselines, policy) -> list:
    host = server["host"]
    heading = Rule(Text("Host " + " · ".join(label(server)), style="bold"), align="left")
    if "error" in found.get(ROOT, {}):
        return [heading, Text(f"{host}: no live read: {found[ROOT]['error']}")]
    model, ids = " ".join(identity(found)), software_ids(found)
    wanted, running = host_baseline(baselines.get(model) or {}, ids), firmware(found)
    can = {c: updatable(found, c) for c in {*update_targets(found), *wanted}}
    # lambda c: a component -> its rollout rank: the BMC first (it performs the other updates), the BIOS next, then
    # the other update targets; one Redfish can't update last
    order = {"Manager (BMC)": 0, "System BIOS": 1}
    components = sorted(can, key=lambda c: (order.get(c, 3 if can[c].startswith("no:") else 2), c))
    outside = [c for c in components if c in wanted and c not in update_targets(found)]  # baselined, not updatable
    results = {c: check(found, baselines, c, policy) for c in components if c in wanted and c not in outside}
    rows = []

    # lambda c: a component -> (push, pull), the pull checked against its catalog image's URL when it has one
    ways = lambda c: ways_in(found, c, (image_for(policy["images"], found, model, c, wanted.get(c)) or {}).get("file")
                             if c in wanted else None) if c not in outside else ("-", "in-band only")
    for c in components:
        version, r = running.get(c, (None, ""))[0], results.get(c)
        if r:
            why = "\n".join([*r["reasons"], *filter(None, r["notes"])]) or "-"
            rows.append((c, text(ids.get(c)), text(version), text(r["want"]), r["direction"], r["updateable"], *ways(c),
                         r["ab"], r["rollback"], r["verdict"], why))
        else:
            why = "update it in-band, from the host OS" if c in outside else "-"
            back = "-" if c in outside else rollback_path(found, c, policy["images"], model, version)
            rows.append((c, text(ids.get(c)), text(version), text(wanted.get(c)), "-", can[c], *ways(c),
                         ab_bank(found, c),
                         back, "-", why))
    system, manager, update = (first_of(found, t) for t in ("ComputerSystem", "Manager", "UpdateService"))
    size = update.get("MaxImageSizeBytes")
    service = ("none" if not update else "disabled" if update.get("ServiceEnabled") is False
               else ", ".join(update_methods(found, update)) or "no update method")
    verdicts = Counter(r["verdict"] for r in results.values())
    readiness = [("Model", model), ("Redfish version", text(found[ROOT].get("RedfishVersion"))),
                 ("Health", f"system {health(system)} · BMC {health(manager)}"), ("Update service", service),
                 ("Max image size", f"{size} bytes ({size / 2**20:.0f} MiB)" if isinstance(size, int) else "-"),
                 ("BMC reset", reset_types(found, manager, "#Manager.Reset")),
                 ("System reset", reset_types(found, system, "#ComputerSystem.Reset")),
                 ("Task service", task_service(found)), ("Jobs to check", str(len(open_jobs(found)))),
                 ("Baselined update targets", f"{len(results)}: {verdicts['go']} go · {verdicts['skip']} skip · "
                                              f"{verdicts['block']} block"),
                 ("Baselined, update in-band", str(len(outside))),
                 ("Read at", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(read_at)))]
    return [heading, block(f"{host} · Update readiness", PROPS, readiness),
            block(f"{host} · Firmware components", HOST_ROW, rows)]


# The overview: one report per host, the hosts grouped by vendor
def overview(servers, reads, baselines, policy, caption) -> list:
    by_vendor = {}
    for s, (found, read_at) in zip(servers, reads):
        by_vendor.setdefault(str(s.get("vendor", "-")), []).append(host_report(s, found, read_at, baselines, policy))
    out = [Text(caption)]
    for vendor, reports in sorted(by_vendor.items()):
        out.append(Rule(Text(f"{vendor} · {len(reports)} host{'s' * (len(reports) > 1)}", style="bold cyan"),
                        align="left"))
        out += [x for report in reports for x in report]
    return out


# Step 1 for every selected host: read it live, pre-flight it, put the hosts that pass in waves
def prepare(args) -> dict:
    """What plan shows and run starts from: servers, reads, results (None without a component), planned waves,
    baselines, policy and when the reads started."""
    servers = [s for s in load_servers(args.inventory) if not args.hosts or s["host"] in args.hosts]
    if not servers:
        raise SystemExit(f"no host of {args.inventory} matches --hosts {' '.join(args.hosts)}")
    baselines = load_baseline(args.baseline)
    policy = {"allow_downgrade": args.allow_downgrade, "accept_no_rollback": args.accept_no_rollback,
              "images": load_images(args.images)}
    tunnels = [tunnel(s) for s in servers]  # fails on missing credentials before any request
    with ThreadPoolExecutor(len(servers)) as pool:
        reads = list(pool.map(read_host, servers, tunnels))
    results = [check(found, baselines, args.component, policy) for found, _ in reads] if args.component else None
    passing = [s for s, r in zip(servers, results or []) if r["verdict"] == "go"]
    rollout = load_rollout(args)
    planned = waves(passing, {s["host"]: r["model"] for s, r in zip(servers, results or [])}, rollout)
    return {"servers": servers, "reads": reads, "results": results, "planned": planned, "baselines": baselines,
            "policy": policy, "rollout": rollout,
            "read_at": time.strftime("%H:%M:%S UTC", time.gmtime(min(t for _, t in reads)))}


# Save the plan for a pipeline's later stages: what to update, from which files, with which options, in which waves
def save_plan(args, servers, results, planned, rollout) -> None:
    """run --plan takes all of it from here, so the stages run what was planned; each host's verdict is kept for the
    record."""
    Path(args.save).write_text(json.dumps({
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "component": args.component,
        "inventory": os.path.relpath(args.inventory), "baseline": os.path.relpath(args.baseline),
        "images": os.path.relpath(args.images),
        "allow_downgrade": args.allow_downgrade, "accept_no_rollback": args.accept_no_rollback, "rollout": rollout,
        "waves": {name: [s["host"] for s in hosts] for name, hosts in planned},
        "verdicts": {s["host"]: {"verdict": r["verdict"], "running": r["running"], "target": r["want"],
                                 "reasons": r["reasons"], "checks": r["checks"], "identity": r.get("identity")}
                     for s, r in zip(servers, results)}}, indent=2))


# Plan: step 1 shown, pre-flight per host and the waves; without a component, a report per host
def plan(args) -> list:
    step = prepare(args)
    servers, reads, results, planned, baselines, policy, read_at, rollout = (
        step[k] for k in ("servers", "reads", "results", "planned", "baselines", "policy", "read_at", "rollout"))
    if args.component is None:
        if args.save:
            raise SystemExit("plan --save needs a component: it saves the waves of one component's update")
        unread = sum("error" in found.get(ROOT, {}) for found, _ in reads)
        caption = (f"{len(servers)} hosts read live from {read_at} · {unread} without a live read · "
                   f"baseline from {os.path.relpath(args.baseline)}")
        return overview(servers, reads, baselines, policy, caption)
    by_vendor = {}
    for s, r in zip(servers, results):
        by_vendor.setdefault(str(s.get("vendor", "-")), []).append(
            (s["host"], str(s.get("project", "-")), r["model"], r["updateable"], r["ab"], r["rollback"],
             text(r["running"]), text(r["want"]), r["direction"], r["verdict"],
             "\n".join([*r["reasons"], *filter(None, r["notes"])]) or "-", r["methods"]))
    counts = Counter(r["verdict"] for r in results)
    caption = (f"{len(servers)} hosts read live from {read_at} · {counts['go']} go · {counts['skip']} skip · "
               f"{counts['block']} block · baseline from {os.path.relpath(args.baseline)}\n"
               f"Rollout policy ({rollout['file'] or 'defaults'}): {rollout_text(rollout)}")
    if args.save:
        save_plan(args, servers, results, planned, rollout)
        caption += f" · saved to {args.save}"
        if counts["block"] and not counts["go"]:  # nothing can go because of blocks: the pipeline stops here
            args.exit_code = 1
            caption += " · every host to update is blocked: a person has to look"
    return [Text(caption),
            *(block(f"Pre-flight · {args.component} · {vendor}", PREFLIGHT_ROW, rows)
              for vendor, rows in sorted(by_vendor.items())),
            block("Plan", PLAN_ROW, [(name, str(len(hosts)), ", ".join(s["host"] for s in hosts))
                                     for name, hosts in planned]) or Text("Plan: no host to update")]


# ---- Run: steps 2 to 6 on each host that passes, wave by wave ----

_record_lock = threading.Lock()


# Append one event to the run's record, runs/<run id>.jsonl: before an action its intent, after it its result
def record(path, host, step, state, **detail) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": host, "step": step,
             "state": state, **detail}
    with _record_lock, open(path, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")
    log.info("%s: %s %s%s", host, step, state, f" · {detail['detail']}" if detail.get("detail") else "")


# A failed answer as text: the Redfish error's first extended message, else its message, with the status
def error_text(status, body) -> str:
    error = obj(body.get("error"))
    extended = [m.get("Message") for m in error.get("@Message.ExtendedInfo") or [] if isinstance(m, dict)]
    return f"HTTP {status or 'no answer'}: {next(filter(None, extended), None) or error.get('message') or '-'}"


# What a component's new firmware takes effect on, so what to reset: (Manager, ComputerSystem or Chassis, its URI)
def activation(found, component) -> tuple[str, str | None]:
    touched = entries(found, component)
    if touched and all(d.get("ResetRequiredOnUpdate") is False for d in touched):
        return "none", None
    owners = [(kind, u) for kind in ("Manager", "ComputerSystem", "Chassis") for u, _ in of(found, kind)]
    for item in (uri_of(r) for d in touched for r in d.get("RelatedItem") or []):
        for kind, u in owners:
            if item and (item == u or item.startswith(u + "/")):
                return kind, u
    kind = "Manager" if component == "Manager (BMC)" else "ComputerSystem"
    return kind, next((u for k, u in owners if k == kind), None)


# The reset type that activates firmware on a manager, system or chassis: the first preferred one it accepts
def reset_type(found, kind, uri) -> str | None:
    allowed = allowed_resets(found, found.get(uri) or {}, f"#{kind}.Reset")
    return next((t for t in RESET_PREFERENCE.get(kind, ()) if allowed is None or t in allowed), None)


# How an image reaches the BMC: pushed to MultipartHttpPushUri when it's a file, or pulled by the BMC with SimpleUpdate
# when it's a URL; {method, action, uri, protocol}, {} when neither can
def handover(found, image) -> dict:
    update, file = first_of(found, "UpdateService"), image.get("file", "")
    simple = obj(obj(update.get("Actions")).get("#UpdateService.SimpleUpdate")).get("target")
    if update.get("MultipartHttpPushUri") and image_path(file):
        return {"method": "push", "action": "MultipartHttpPushUri", "uri": update["MultipartHttpPushUri"]}
    if simple and "://" in file:
        return {"method": "pull", "action": "#UpdateService.SimpleUpdate", "uri": simple,
                "protocol": file.split("://")[0].upper()}
    return {}


# Step 3: hand the image to the BMC, to install at the apply time asked; ((task monitor, task), error), None if none
def start_update(tunnel, found, component, image, apply_time, cut=None) -> tuple[tuple, str | None]:
    file, targets, how = image.get("file", ""), update_targets_of(found, component), handover(found, image)
    params = {**({"Targets": targets} if targets else {}), "@Redfish.OperationApplyTime": apply_time}
    if how.get("method") == "push":
        with open(image_path(file), "rb") as f:
            # urllib3 applies the first timeout while it sends the body, so it has to cover the whole upload
            status, headers, body = send(tunnel, "POST", how["uri"], timeout=(900, 900), files={
                "UpdateParameters": (None, json.dumps(params), "application/json"),
                "UpdateFile": (image_path(file).name, f.read(cut) if cut else f, "application/octet-stream")})
    elif how.get("method") == "pull":
        body = {"ImageURI": file, "TransferProtocol": how["protocol"], **params}
        status, headers, body = send(tunnel, "POST", how["uri"], json=body)
    else:
        return (None, None), "no way to hand this image over: multipart push needs a file, SimpleUpdate a URL"
    if status == 202:
        task = uri_of(body) if body.get("TaskState") else None  # a BMC may answer with the Task itself
        return (headers.get("Location") or body.get("TaskMonitor") or task, task), None
    return (None, None), (None if status in (200, 201, 204) else error_text(status, body))


# A Task resource's standard properties, as the record keeps them
def task_of(uri, body) -> dict:
    messages = [{"severity": m.get("MessageSeverity") or m.get("Severity"), "message_id": m.get("MessageId"),
                 "message": m.get("Message")} for m in body.get("Messages") or [] if isinstance(m, dict)]
    return {"uri": uri, "state": body.get("TaskState"), "status": body.get("TaskStatus"),
            "percent": body.get("PercentComplete"), "start": body.get("StartTime"), "end": body.get("EndTime"),
            "messages": messages}


# Follow an update task until it ends: (end state, its last message, the task as last read)
def follow_task(tunnel, monitor, task_uri, timeout, changed) -> tuple[str, str, dict]:
    """The Task resource carries the state, PercentComplete and messages; the monitor only says whether it's done, so
    it's polled only when the BMC names no task. changed(task) is called on every change of state, percent or
    messages, so the record follows the task."""
    if not (monitor or task_uri):
        return "Completed", "accepted without a task", {}
    until, task, seen = time.monotonic() + timeout, {}, None
    while time.monotonic() < until:
        status, headers, body = send(tunnel, "GET", task_uri or monitor)
        if status in (0, 401, 404, 410):
            return "gone", f"task answered {status or 'nothing'}", task
        if body.get("TaskState"):
            task_uri = task_uri or uri_of(body)
            task = task_of(task_uri or monitor, body)
        state = body.get("TaskState") or ("Exception" if "error" in body else "Completed" if status != 202 else None)
        if state == "Completed" and body.get("TaskStatus") == "Critical":
            state = "Exception"
        if task and (task["state"], task["percent"], len(task["messages"])) != seen:
            seen = task["state"], task["percent"], len(task["messages"])
            changed(task)
        if state in ("Completed", "Exception", "Killed", "Cancelled"):
            last = task["messages"][-1] if task.get("messages") else None
            return state, f"{last['message_id']}: {last['message']}" if last else "-", task
        time.sleep(min(int(headers.get("Retry-After") or 5), 30))
    return "timeout", f"no end after {timeout}s", task


# Wait for the BMC after reset
def wait_back(server, timeout, grace=180) -> tuple[bool, float]:
    root, start, down = f"{server['scheme']}://{server['host']}{ROOT}", time.monotonic(), False
    while time.monotonic() - start < timeout:
        try:
            code = requests.get(root, verify=server.get("verify", True), timeout=5).status_code
        except requests.RequestException:
            code = 0
        down = down or code != 200
        if code == 200 and (down or time.monotonic() - start > grace):
            return True, time.monotonic() - start
        time.sleep(5)
    return False, time.monotonic() - start


# Wait for a system after its reset: PowerState On and, where reported, BootProgress OSRunning; (back, last seen)
def wait_host(server, uri, timeout) -> tuple[bool, str]:
    time.sleep(60)
    until, seen = time.monotonic() + timeout, "-"
    while time.monotonic() < until:
        _, _, doc = send(tunnel(server), "GET", uri)
        power, boot = doc.get("PowerState"), obj(doc.get("BootProgress")).get("LastState")
        seen = f"PowerState {text(power)}, BootProgress {text(boot)}"
        if power == "On" and boot in (None, "OSRunning"):
            return True, seen
        time.sleep(10)
    return False, seen


# Step 4: reset what activates the new firmware, then wait until it's back; (ok, detail)
def reset(server, found, kind, uri, restarted, timeout) -> tuple[bool, str]:
    """restarted: the BMC already restarted by itself during the update (apply time Immediate); only wait for it."""
    if kind == "none":
        return True, "no reset needed: ResetRequiredOnUpdate false"
    if not restarted:
        rtype = reset_type(found, kind, uri) if uri else None
        target = obj(obj(obj(found.get(uri)).get("Actions")).get(f"#{kind}.Reset")).get("target")
        if not (rtype and target):
            return False, f"no {kind}.Reset to activate the firmware with"
        link_ = tunnel(server)
        location = login(link_)
        status, _, body = send(link_, "POST", target, json={"ResetType": rtype})
        if kind != "Manager" and location:
            logout(link_, location)
        if status not in (200, 202, 204):
            return False, f"{kind}.Reset {rtype}: {error_text(status, body)}"
    if kind == "Manager":
        back, seconds = wait_back(server, timeout)
        return back, (f"BMC back after {seconds:.0f}s" if back else f"BMC not back after {timeout}s")
    return wait_host(server, uri, timeout)


# The state post-checks compare against: running version, the health of every system and manager, open jobs
def before_state(found, running) -> dict:
    return {"version": text(running), "jobs": [j[0] for j in open_jobs(found)],
            "health": {u: health(d) for kind in ("ComputerSystem", "Manager") for u, d in of(found, kind)}}


# Step 5: the checks after the reset, on a fresh live read; (passed, [(check, passed, detail)], the version running)
def post_check(server, key, want, before) -> tuple[bool, list[dict], str]:
    """A BMC that can't be read fails them all. A BMC answers before it is ready: until its software manager fills the
    firmware inventory, the running version isn't reported, so that is read again, every 10 s for up to 3 min, before
    it counts. A version that is reported and wrong fails at once."""
    until = time.monotonic() + 180
    while True:
        found, _ = read_host(server, tunnel(server))
        if "error" in found.get(ROOT, {}):
            return False, [{"check": "live read", "ok": False, "detail": found[ROOT]["error"]}], "-"
        running = text(firmware(found).get(resolve(found, key), (None, ""))[0])
        if running != "-" or time.monotonic() > until:
            break
        time.sleep(10)
    return after_checks(found, key, want, before)


# The post-check on one read: the version asked for now runs; every system and manager is OK, or no worse than
# before; no new job or task failed or hangs
def after_checks(found, key, want, before) -> tuple[bool, list[dict], str]:
    running = text(firmware(found).get(resolve(found, key), (None, ""))[0])
    now = {u: health(d) for kind in ("ComputerSystem", "Manager") for u, d in of(found, kind)}
    was = before["health"]
    jobs = [j for j in open_jobs(found) if j[0] not in before["jobs"]]
    checks = [{"check": "running version", "ok": running == want, "detail": f"{running}, target {want}"},
              *({"check": "health", "ok": h == "OK" or h == was.get(u), "detail": f"{u}: {h}, before {was.get(u, '-')}"}
                for u, h in now.items()),
              {"check": "new jobs and tasks", "ok": not jobs,
               "detail": ", ".join(f"{j[1]}: {j[2]}" for j in jobs) or "none"}]
    return all(c["ok"] for c in checks), checks, running


# Steps 3 to 5 for one image: update, reset, post-check; (passed, activated, what happened), each step recorded
def apply(server, found, key, image, want, before, policy, rec, phase) -> tuple[bool, bool, str]:
    """activated: the reset ran, so the firmware running may have changed and a failure then needs a rollback."""
    host, component = server["host"], resolve(found, key)
    kind, target = activation(found, component)
    link_ = tunnel(server)
    location = login(link_)
    fault = policy.get("fault")
    noted = lambda *kinds: {"fault": f"{fault}: {FAULTS[fault]}"} if fault in kinds else {}  # where a fault acts
    rec(host, f"{phase}update", "start", **handover(found, image), image={k: image.get(k) for k in ("file", "sha256")},
        target=want, apply_time=policy["apply_time"], targets=update_targets_of(found, component),
        **noted("silent-fail", "rejected") if not phase else {})
    cut = 8 * 2**20 if fault == "rejected" and not phase else None
    (monitor, task_uri), error = start_update(link_, found, component, image, policy["apply_time"], cut)
    # lambda task: the task as just read -> one record event, so the record follows it through every change
    changed = lambda task: rec(host, f"{phase}update", "task", task=task, detail=task_line(task))
    state, message, task = (("failed", error, {}) if error
                            else follow_task(link_, monitor, task_uri, policy["task_timeout"], changed))
    rec(host, f"{phase}update", state, detail=message, task=task)
    if location and state != "gone":
        logout(link_, location)
    if state not in ("Completed", "gone"):
        return False, False, f"{phase}update {state}: {message}"
    rec(host, f"{phase}reset", "start", action=f"#{kind}.Reset" if kind != "none" else None, resource=target,
        reset_type=reset_type(found, kind, target) if target else None, restarted=state == "gone",
        **noted("no-return"))
    ok, detail = reset(server, found, kind, target, state == "gone",
                       45 if fault == "no-return" else policy["reset_timeout"])
    rec(host, f"{phase}reset", "done" if ok else "failed", detail=detail)
    if not ok:
        return False, True, f"{phase}reset: {detail}"
    passed, checks, running = post_check(server, key, want, before)
    if fault == "unhealthy" and not phase:
        checks, passed = [*checks, {"check": "injected fault", "ok": False, "detail": FAULTS[fault]}], False
    rec(host, f"{phase}post-check", "passed" if passed else "failed", checks=checks, running=running, target=want)
    failed = "; ".join(f"{c['check']}: {c['detail']}" for c in checks if not c["ok"])
    return passed, True, f"{phase}post-check " + ("passed" if passed else f"failed ({failed})")


# Step 2 and the undrain of step 6: take a host out of the scheduler or give it back; (ok, output)
def scheduler(command, server) -> tuple[bool, str]:
    """The --drain or --undrain command with {host} and {node} (the inventory's node, else the host) filled in; it
    returns once done, a drain once the node is empty. None configured: nothing to do, as in the lab."""
    if not command:
        return True, "no scheduler configured"
    try:
        argv = shlex.split(command.format(host=server["host"], node=server.get("node", server["host"])))
        p = subprocess.run(argv, capture_output=True, text=True, timeout=24 * 3600)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:] or f"exit {p.returncode}"


# One host through steps 2 to 6, read and pre-flighted again first; its outcome for the run table
def run_host(server, key, baselines, policy, rec) -> dict:
    """A drain only when the reset touches the host (a system or chassis reset). A failure before the reset leaves
    the old firmware running: undrain. After it: roll back by reinstalling the version that ran before, check again,
    undrain. A failed rollback, or no way to roll back, leaves the host drained for a person: needs_attention."""
    host, started = server["host"], time.monotonic()
    found, _ = read_host(server, tunnel(server))
    r = check(found, baselines, key, policy)
    rec(host, "pre-flight", r["verdict"], checks=r["checks"], reasons=r["reasons"], running=r["running"],
        target=r["want"], identity=r.get("identity"))
    out = {"host": host, "from": text(r["running"]), "now": text(r["running"])}
    if r["verdict"] != "go":
        return {**out, "state": "skipped" if r["verdict"] == "skip" else "blocked", "detail": "; ".join(r["reasons"]),
                "seconds": time.monotonic() - started}
    component, want = r["component"], r["want"]
    before = before_state(found, r["running"])
    drain = activation(found, component)[0] in ("ComputerSystem", "Chassis")
    if drain:
        rec(host, "drain", "start", command=policy["drain"])
        ok, output = scheduler(policy["drain"], server)
        rec(host, "drain", "done" if ok else "failed", detail=output)
        if not ok:
            return {**out, "state": "failed", "detail": f"drain: {output}", "seconds": time.monotonic() - started}
    else:
        rec(host, "drain", "skipped", detail="not needed: the reset leaves the host running")
    policy = {**policy, "fault": policy["faults"].get(host) or policy["faults"].get("*")}
    image = image_for(policy["images"], found, r["model"], component,
                      before["version"] if policy["fault"] == "silent-fail" else want)
    passed, activated, detail = apply(server, found, key, image, want, before, policy, rec, "")
    state = "updated" if passed else "failed"
    if not passed and activated:
        back = image_for(policy["images"], found, r["model"], component, before["version"])
        if not r["rollback"].startswith("reinstall") or not back:
            state, detail = "needs_attention", f"{detail}; rollback: {r['rollback']} (switching banks isn't built)"
        else:
            rec(host, "rollback", "start", to=before["version"], reason=detail)
            fresh, _ = read_host(server, tunnel(server))
            rolled, _, how = apply(server, found if "error" in fresh.get(ROOT, {}) else fresh, key, back,
                                   before["version"], before, policy, rec, "rollback ")
            rec(host, "rollback", "done" if rolled else "failed", verified=rolled, detail=how)
            state, detail = ("rolled_back" if rolled else "needs_attention"), f"{detail}; {how}"
    if drain and state != "needs_attention":
        rec(host, "undrain", "start", command=policy["undrain"])
        ok, output = scheduler(policy["undrain"], server)
        rec(host, "undrain", "done" if ok else "failed", detail=output)
    else:
        rec(host, "undrain", "skipped", detail="kept drained for a person" if drain else "nothing was drained")
    now, _ = read_host(server, tunnel(server))
    running = firmware(now).get(resolve(now, key), (None, ""))[0] if "error" not in now.get(ROOT, {}) else None
    rec(host, "done", state, detail=detail, running=running, target=want, before=before["version"])
    return {**out, "state": state, "now": text(running), "detail": detail, "seconds": time.monotonic() - started,
            "server": server, "before": before, "target": want}


# What run would do on a host that passes, for the dry run: one row of the actions table
def actions(server, found, r, policy, letter) -> tuple[str, ...]:
    """letter: {version: its letter in the Versions table above, which names each version's package}"""
    component = r["component"]
    kind, uri = activation(found, component)
    how = handover(found, image_for(policy["images"], found, r["model"], component, r["want"]) or {})
    drain = (f"yes: {policy['drain'] or 'no scheduler configured'}" if kind in ("ComputerSystem", "Chassis")
             else "no: the host keeps running")
    rtype = reset_type(found, kind, uri) if uri else None
    targets = ", ".join(update_targets_of(found, component)) or "BMC decides"
    back = r["rollback"]
    return (server["host"], component, f"{letter[text(r['running'])]} → {letter[r['want']]}", drain,
            f"{how['method']} · {policy['apply_time']} · to {targets}" if how else "no way to hand it over",
            f"{kind}.Reset {text(rtype)}" if kind != "none" else "none needed",
            f"reinstall {letter[text(r['running'])]}" if back.startswith("reinstall") else back)


# The versions a run moves between, a letter each, with the package the catalog has for each: the target's is the one
# pushed, the running one's is the rollback's
def versions_table(go, images) -> tuple[dict, Table | list]:
    """go: (server, found, pre-flight result) of every host the run will update"""
    targets = list(dict.fromkeys(r["want"] for _, _, r in go))
    running = Counter(text(r["running"]) for _, _, r in go)
    letter = {v: chr(ord("A") + i) for i, v in enumerate(dict.fromkeys([*targets, *running]))}
    package = {}
    for _, found, r in go:
        for v in (r["want"], text(r["running"])):
            image = image_for(images, found, r["model"], r["component"], v) or {}
            package.setdefault(v, f"{image['file']} (sha256 {str(image.get('sha256'))[:12]}…)" if image else "none")
    rows = [(letter[v], v, "target" if v in targets else "running now", str(running[v]), package[v]) for v in letter]
    return letter, block("Versions", VERSIONS_ROW, rows, expand=False)


# The soak after a wave: wait, then check its updated hosts again as the post-check did; a host that went bad since is
# recorded and ends needs_attention. The hosts that failed it
def soak(done, key, wait, rec) -> list[dict]:
    time.sleep(wait)
    soured = []
    for o in (o for o in done if o["state"] == "updated"):
        passed, checks, running = post_check(o["server"], key, o["target"], o["before"])
        rec(o["host"], "soak", "passed" if passed else "failed", checks=checks, running=running, target=o["target"],
            waited=wait)
        if not passed:
            o["state"] = "needs_attention"
            rec(o["host"], "done", "needs_attention", detail=f"soak: failed {wait}s after the update", running=running,
                target=o["target"], before=o["before"]["version"])
            soured.append(o)
    return soured


# The gate after a wave: any failure halts the canary, a strict wave, or a wave with a host that failed its soak; a
# later wave halts when more than halt_at of the hosts tried so far failed. states: every host so far, every wave
def gate(wave, states, soured, rollout) -> dict:
    strict = wave == "canary" or wave in {f"wave {n}" for n in range(1, rollout["strict_waves"] + 1)}
    tried = [s for s in states if s not in ("skipped", "blocked")]
    failed = [s for s in tried if s in FAILED_STATES]
    halted = bool(failed) and (strict or soured > 0 or len(failed) / len(tried) > rollout["halt_at"])
    return {"halted": halted, "failed": len(failed), "tried": len(tried), "strict": strict}


# Run: step 1, then steps 2 to 6 wave by wave; without --yes, the dry run
def run(args) -> list:
    """With --plan the saved plan decides the component, files, options, rollout policy and waves, and --stage runs the
    canary or the waves after it. args.exit_code is 1 when a gate fails: a failure in the canary or a strict wave, a
    soak check that failed, or more than halt_at of the hosts tried failed."""
    args.exit_code, saved = 0, json.loads(Path(args.plan).read_text()) if args.plan else None
    if saved:
        for key in ("component", "inventory", "baseline", "images", "allow_downgrade", "accept_no_rollback"):
            setattr(args, key, saved[key])
        stage = {name: hosts for name, hosts in saved["waves"].items()
                 if args.stage == "all" or (name == "canary") == (args.stage == "canary")}
        args.hosts = [h for hosts in stage.values() for h in hosts]
        if not args.hosts:
            return [Text(f"Nothing to do: the plan has no host for the {args.stage} stage")]
    if not args.component:
        raise SystemExit("run needs a component, or --plan with a saved plan")
    step = prepare(args)
    servers, reads, results, planned, baselines, policy, rollout = (
        step[k] for k in ("servers", "reads", "results", "planned", "baselines", "policy", "rollout"))
    if saved:
        rollout = {**saved["rollout"], **({"halt_at": args.halt_at} if args.halt_at is not None else {})}
        by_host = {s["host"]: s for s in servers}
        planned = [(name, [by_host[h] for h in hosts if h in by_host]) for name, hosts in stage.items()]
    policy.update(faults=dict(args.fault or []), apply_time=APPLY_TIME, drain=args.drain, undrain=args.undrain,
                  task_timeout=TASK_TIMEOUT, reset_timeout=args.reset_timeout)
    counts = Counter(r["verdict"] for r in results)
    faults = f"\nFaults injected: {', '.join(f'{h}={k}' for h, k in args.fault)}" if args.fault else ""
    caption = (f"{len(servers)} hosts read live from {step['read_at']} · {counts['go']} go · {counts['skip']} "
               f"skip · {counts['block']} block\nRollout policy: {rollout_text(rollout)}{faults}")
    found_of = {s["host"]: (found, r) for s, (found, _), r in zip(servers, reads, results)}
    go = [(s, *found_of[s["host"]]) for _, hosts in planned for s in hosts]
    letter, legend = versions_table(go, policy["images"])
    rows = [(name, *actions(s, *found_of[s["host"]], policy, letter)) for name, hosts in planned for s in hosts]
    skipped = [f"{s['host']}: {r['verdict']}, {'; '.join(r['reasons'])}" for s, r in zip(servers, results)
               if r["verdict"] != "go"]
    plan_view = [Text(caption), *([legend, block("Actions per host", ACTIONS_ROW, rows)] if rows
                                  else [Text("Nothing to do: no host passes")])]
    if not args.yes:
        return [*plan_view, Text("\n".join([*skipped, "Dry run: nothing was changed. Add --yes to run it."]))]
    refused = [s["host"] for name, hosts in planned for s in hosts if s.get("writable") is not True]
    if refused:
        raise SystemExit(f"not writable in {args.inventory}: {', '.join(refused)}. Only hosts marked writable: true "
                         "are updated; production stays read-only.")
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    pipeline = Path(args.plan).parent.name if args.plan else path.stem
    group, pushing = f"site/{SITE.name}/pipeline/{pipeline}/stage/{args.stage}", {}

    # lambda *event, **detail: record one event, then push the run's metrics as they stand
    def rec(*event, **detail) -> None:
        record(path, *event, **detail)
        push_metrics(path, group, pushing)
    rec("-", "run", "start", component=args.component, waves={name: [s["host"] for s in h] for name, h in planned},
        plan=args.plan, stage=args.stage, halt_at=rollout["halt_at"], rollout=rollout, apply_time=APPLY_TIME,
        faults=policy["faults"])
    last = list(saved["waves"])[-1] if saved else planned[-1][0]
    outcomes, halted = [], None
    for name, hosts in planned:
        with ThreadPoolExecutor(min(len(hosts), rollout["max_parallel"] or len(hosts))) as pool:
            # lambda s: a server of the wave -> its outcome; the wave's hosts run side by side, max_parallel at once
            done = list(pool.map(lambda s: run_host(s, args.component, baselines, policy, rec), hosts))
        soured = []
        if rollout["soak"] and name != last and not any(o["state"] in FAILED_STATES for o in done):
            soured = soak(done, args.component, rollout["soak"], rec)
        outcomes += [(name, o) for o in done]
        g = gate(name, [o["state"] for _, o in outcomes], len(soured), rollout)
        rec("-", "gate", "halted" if g["halted"] else "passed", wave=name, failed=g["failed"], tried=g["tried"],
            limit=rollout["halt_at"], strict=g["strict"], soak_failed=len(soured))
        if g["halted"]:
            halted = f"halted after {'the ' * (name == 'canary')}{name}: {g['failed']} of {g['tried']} hosts failed"
            args.exit_code = 1
            break
    rec("-", "run", "end", detail=halted or "every wave done")
    return [*plan_view, *render(pipeline_report(None, events_of([path]), path.stem, [path])), Text(f"Record: {path}")]


# ---- Metrics: a run's report as Prometheus metrics, pushed while it runs when PUSHGATEWAY is set ----

# The report as Prometheus text: hosts by wave and state, each host's versions and time, each step's duration, every
# check after the flash (post-check, soak, a rollback's post-check), each gate's decision, and whether the run goes on
def metrics(doc) -> str:
    lines = []

    # lambda name, value, **labels: add one sample
    add = lambda name, value, **labels: lines.append(sample(name, value, **labels))

    for w in doc["waves"]:
        for state, n in w["totals"].items():
            add("rollout_hosts", n, wave=w["name"], state=state)
        g = w["gate"]
        if g.get("tried") is not None:
            add("rollout_gate_failed", g["failed"], wave=w["name"])
            add("rollout_gate_tried", g["tried"], wave=w["name"])
            add("rollout_gate_halted", int(g["result"] == "halted"), wave=w["name"])
        for d in w["hosts"]:
            add("rollout_host_info", 1, wave=w["name"], host=d["host"], state=d["state"], before=text(d["before"]),
                   target=text(d["target"]), after=text(d["after"]))
            if d["seconds"] is not None:
                add("rollout_host_seconds", d["seconds"], wave=w["name"], host=d["host"])
            for s in d["steps"]:
                if s.get("seconds") is not None:
                    add("rollout_step_seconds", s["seconds"], host=d["host"], step=s["step"])
                if s["step"] in ("post-check", "soak", "rollback post-check"):
                    for c in s.get("checks") or []:
                        add("rollout_check_ok", int(c["ok"]), host=d["host"], step=s["step"], check=c["check"],
                               detail=c["detail"])
    add("rollout_running", int(doc["result"] == "running"))
    return "\n".join(lines) + "\n"


_push_lock = threading.Lock()


# Push a run's metrics to the Pushgateway, if PUSHGATEWAY is set: best effort, a rollout never waits on its monitoring.
# Each push replaces the run's group (pipeline, stage) with the report as the record stands now
def push_metrics(path, group, state) -> None:
    url = os.environ.get("PUSHGATEWAY")
    if not url or state.get("off"):
        return
    with _push_lock:
        try:
            body = metrics(pipeline_report(None, events_of([path]), path.stem, [path]))
            requests.put(f"{url.rstrip('/')}/metrics/job/rollout/{group}", data=body, timeout=2).raise_for_status()
        except Exception as e: 
            state["off"] = True
            log.warning("metrics: %s; no more pushes this run", reason(str(e)))


# ---- Report: one document per pipeline, from its plan and its run records; the views for people are drawn from it --

# Seconds between two times of a record; None without both
def seconds(start, end) -> int | None:
    if not (start and end):
        return None
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


# A duration for people: 38s, 4m38s, 1h02m
def took(secs) -> str:
    if secs is None:
        return "-"
    return f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60:02d}s" if secs < 3600 else \
        f"{secs // 3600}h{secs % 3600 // 60:02d}m"


# The events of run records, merged in the order they happened
def events_of(paths) -> list[dict]:
    # lambda e: a recorded event -> its time
    return sorted((json.loads(line) for path in paths for line in open(path)), key=lambda e: e["at"])


# A host's steps in the order they began: each start merged with its result, and a task's progress under its step
def steps_of(events) -> list[dict]:
    steps, open_ = [], {}
    for e in events:
        fields = {k: v for k, v in e.items() if k not in ("at", "host", "step", "state")}
        if isinstance(obj(fields.get("task")).get("messages"), list):  # records before the messages were named
            fields["task"] = {**fields["task"], "messages": [m if isinstance(m, dict) else dict(zip(
                ("severity", "message_id", "message"), m)) for m in fields["task"]["messages"]]}
        if e["state"] == "task":
            if e["step"] in open_:
                open_[e["step"]].setdefault("progress", []).append(
                    {"at": e["at"], "state": e["task"].get("state"), "percent": e["task"].get("percent")})
            continue
        if e["state"] == "start":
            open_[e["step"]] = {"step": e["step"], "result": "running", "started": e["at"], "ended": None,
                                "seconds": None, **fields}
            steps.append(open_[e["step"]])
            continue
        step = open_.pop(e["step"], None)
        if step is None:  # recorded once, when it ended: pre-flight, post-check
            step = {"step": e["step"], "started": e["at"]}
            steps.append(step)
        step.update(fields, result=e["state"], ended=e["at"], seconds=seconds(step["started"], e["at"]))
    return steps


# How one host went: its end state, the versions before and after, how long it took, why, what next, and its steps
def outcome(host, wave, events, seen, live=False) -> dict:
    """seen: the plan's pre-flight of the host, for a host the run never read again. live: its run is still going, so a
    host without an end is running, not stopped halfway. after: the version read at the end, None when the run didn't
    touch the host or couldn't read it."""
    pre = next((e for e in events if e["step"] == "pre-flight"), {})
    done = next((e for e in reversed(events) if e["step"] == "done"), None)
    verdict = pre.get("state") or seen.get("verdict")
    state = (done["state"] if done else {"block": "blocked", "skip": "skipped"}.get(verdict)
             or (("running" if live else "incomplete") if events else "untouched"))
    steps = steps_of([e for e in events if e["step"] != "done"])
    why = (done or {}).get("detail") if state != "updated" else None
    if state in ("blocked", "skipped"):
        why = "; ".join(pre.get("reasons") or seen.get("reasons") or [])
    after_next = NEXT_STEP.get(state)
    if after_next and any(s["step"].endswith("update") and s.get("result") == "Exception" for s in steps):
        after_next += "; the BMC keeps the failed task until it restarts, and pre-flight blocks it till then"
    return {"host": host, "wave": wave, "state": state, "identity": pre.get("identity") or seen.get("identity"),
            "before": pre.get("running") or seen.get("running"), "target": pre.get("target") or seen.get("target"),
            "after": (done or {}).get("running"), "started": events[0]["at"] if events else None,
            "ended": events[-1]["at"] if events else None,
            "seconds": seconds(events[0]["at"], events[-1]["at"]) if events else None,
            "why": why or None, "next": after_next, "steps": steps}


# Why a gate halted, from its event: a strict wave (the canary, the first waves) may not fail at all, a soak check
# that failed stops the next wave, and later waves may fail up to the policy's share
def gate_rule(gate) -> str:
    if gate.get("soak_failed"):
        return f"{gate['soak_failed']} hosts failed the soak check"
    if gate.get("strict", gate["wave"] == "canary"):
        return f"{gate['wave']} may not have a failure"
    return f"no more than {gate['limit']:.0%} of the hosts tried may fail"


# The report of a pipeline, or of one run without a plan: how it ended and why, every host by wave with its gate, the
# hosts the plan left out, totals, the fleet's versions before and after, and what needs a person
def pipeline_report(plan, events, name, records) -> dict:
    runs = [e for e in events if e["host"] == "-"]
    starts = [e for e in runs if e["step"] == "run" and e["state"] == "start"]
    gates = {e["wave"]: e for e in runs if e["step"] == "gate"}
    first = starts[0] if starts else {}
    live = len(starts) > sum(e["step"] == "run" and e["state"] == "end" for e in runs)  # a run started, not ended
    waves = plan["waves"] if plan else {w: hosts for s in starts for w, hosts in s["waves"].items()}
    seen = (plan or {}).get("verdicts", {})
    by_host = {}
    for e in events:
        by_host.setdefault(e["host"], []).append(e)
    wave_docs, halted = [], None
    for wave, hosts in waves.items():
        docs, gate = [outcome(h, wave, by_host.get(h, []), seen.get(h, {}), live) for h in hosts], gates.get(wave)
        for d in docs:
            if d["state"] == "untouched":
                d["why"] = f"halted after {halted}: its wave never started" if halted else "its wave hasn't run yet"
        wave_docs.append({"name": wave, "totals": dict(Counter(d["state"] for d in docs)), "hosts": docs,
                          "gate": {k: gate.get(k) for k in ("failed", "tried", "limit", "strict", "soak_failed")}
                          | {"result": gate["state"]}
                          if gate else {"result": "not run" if halted else "pending"}})
        halted = halted or (wave if gate and gate["state"] == "halted" else None)
    left_out = [outcome(h, None, by_host.get(h, []), v) for h, v in seen.items() if v["verdict"] != "go"]
    everyone = [d for w in wave_docs for d in w["hosts"]] + left_out
    if halted:
        result = "halted"
    elif live:
        result = "running"
    elif wave_docs and all(w["gate"]["result"] == "passed" for w in wave_docs):
        result = "passed"
    elif not wave_docs:
        result = "blocked" if any(v["verdict"] == "block" for v in seen.values()) else "nothing to do"
    else:
        result = "incomplete"
    stop = gates.get(halted, {})
    started = events[0]["at"] if events else (plan or {}).get("created")
    ended = events[-1]["at"] if events else started
    return {
        "schema": REPORT_SCHEMA, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline": name, "site": SITE.name, "component": (plan or {}).get("component") or first.get("component"),
        "targets": sorted({d["target"] for d in everyone if d["target"]}),
        "plan": {k: plan.get(k) for k in ("created", "inventory", "baseline", "images", "allow_downgrade",
                                          "accept_no_rollback", "rollout")} if plan else None,
        "run": {"halt_at": first.get("halt_at"), "apply_time": first.get("apply_time"),
                "faults": first.get("faults") or {}},
        "records": [Path(r).name for r in records], "started": started, "ended": ended,
        "seconds": seconds(started, ended), "result": result,
        "stopped": {"after": halted, "failed": stop["failed"], "tried": stop["tried"], "limit": stop["limit"],
                    "rule": gate_rule(stop), "not_run": [w["name"] for w in wave_docs if
                                                          w["gate"]["result"] == "not run"]} if halted else None,
        "totals": dict(Counter(d["state"] for d in everyone)),
        "fleet": {"before": dict(Counter(d["before"] or "unknown" for d in everyone)),
                  "after": dict(Counter(d["after"] or (d["before"] if d["state"] in ("untouched", "blocked", "skipped",
                                                                                     "running") else None)
                                        or "unknown" for d in everyone))},
        "action_needed": [{k: d[k] for k in ("host", "wave", "state", "why", "next")} for d in everyone
                          if d["state"] in NEXT_STEP],
        "waves": wave_docs, "left_out": left_out}


# A task's progress
def trail(progress) -> str:
    parts, last = [], None
    for p in progress:
        percent = f" {p['percent']}%" if p.get("percent") is not None else ""
        parts.append(p["at"][11:19] + (f" {p['state']}" if p["state"] != last else "") + percent)
        last = p["state"]
    return "progress " + " → ".join(parts) if parts else ""


# A task in one line: state, status and percent, and its last message: "Running, OK, 45% · Update.1.0.Applying"
def task_line(task) -> str:
    head = ", ".join(filter(None, (task.get("state"), task.get("status"),
                                   f"{task['percent']}%" if task.get("percent") is not None else None)))
    return " · ".join(filter(None, (head, task["messages"][-1]["message_id"] if task.get("messages") else None)))


# The task an update ran as: its URI, how it ended and when, then every message it gave, repeats counted
def task_detail(task) -> list[str]:
    times = " → ".join(t[11:19] for t in (task.get("start"), task.get("end")) if t)
    lines = [f"task {task['uri']}: " + ", ".join(filter(None, (task.get("state"), task.get("status"),
             f"{task['percent']}%" if task.get("percent") is not None else None, times and f"{times} UTC")))]
    runs = []  # [severity, id, message, count]: a message the BMC repeats (progress) counts once
    for severity, mid, message in (m.values() for m in task.get("messages") or []):
        if runs and runs[-1][1] == mid:
            runs[-1][2:] = message, runs[-1][3] + 1
        else:
            runs.append([severity, mid, message, 1])
    return lines + [f"  {sev} {mid}{f' ×{n}' if n > 1 else ''}: {message}" for sev, mid, message, n in runs]


# One step with detail per step.
def step_detail(step) -> str:
    lines, kind = [f"fault injected · {step['fault']}"] if step.get("fault") else [], step["step"]
    if kind == "rollback":
        lines += [f"why: {step.get('reason') or '-'}", f"reinstall {step.get('to')}",
                  "✓ verified back on it" if step.get("verified") else "✗ not verified back on it"]
    elif kind.endswith("update") and step.get("method"):
        image = step.get("image") or {}
        lines += [f"{step['method']} · {step['action']} {step['uri']}" + (f" · {step['protocol']}"
                                                                         if step.get("protocol") else ""),
                  f"{image.get('file')} (sha256 {str(image.get('sha256'))[:12]}…) → {step.get('target')}",
                  f"apply {step.get('apply_time')} · targets {', '.join(step.get('targets') or []) or 'BMC decides'}"]
        lines += [trail(step.get("progress") or []), *task_detail(step["task"])] if step.get("task") else []
    elif kind.endswith("reset") and "action" in step:
        lines.append(f"{step['action']} {step.get('reset_type')} on {step.get('resource')}" if step["action"]
                     else "no reset needed: ResetRequiredOnUpdate false")
        lines += ["the BMC restarted by itself during the update"] if step.get("restarted") else []
    elif "command" in step:
        lines.append(step["command"] or "no scheduler configured")
    lines += [f"{'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}" for c in step.get("checks") or []]
    if step.get("detail") and step["detail"] != "-" and not obj(step.get("task")).get("messages"):
        lines.append(str(step["detail"]))
    return "\n".join(filter(None, lines)) or "-"


# The report for people: the verdict and what needs a person first, the versions, every host by wave with each wave's
# gate, then the hosts left out; with details, every host's steps and their evidence (host: that host's only)
def render(doc, details=False, host=None) -> list:
    plan, run, stop = doc["plan"] or {}, doc["run"], doc["stopped"]
    when = f"{(doc['started'] or '-')[11:19]} → {(doc['ended'] or '-')[11:19]} UTC ({took(doc['seconds'])})"
    rules = [f"policy: {rollout_text(plan['rollout'])}" if plan.get("rollout") else None,
             f"apply {run['apply_time']}" if run["apply_time"] else None]
    lines = [Text(f"Rollout {doc['pipeline']} · {doc['site']} · {doc['component']}", style="bold"),
             Text(" · ".join(filter(None, (when, *rules))))]
    if run["faults"]:
        faults = ", ".join(f"{h} {k}" for h, k in run["faults"].items())
        lines.append(Text(f"Faults injected (lab testing): {faults}", style="yellow"))
    if stop:
        not_run = f"; not run: {', '.join(stop['not_run'])}" if stop["not_run"] else ""
        halted = f"HALTED after {stop['after']}: {stop['failed']} of {stop['tried']} failed, {stop['rule']}{not_run}"
    verdict = {"passed": ("PASSED: every wave's gate passed", "bold green"),
               "halted": (halted if stop else "HALTED", "bold red"),
               "blocked": ("BLOCKED at the plan: every host to update is blocked", "bold red"),
               "running": ("RUNNING: the pipeline isn't done yet", "yellow"),
               "incomplete": ("INCOMPLETE: a stage stopped in the middle, or hasn't run yet", "yellow"),
               "nothing to do": ("NOTHING TO DO: every host is on the baseline", "green")}[doc["result"]]
    lines += [Text(verdict[0], style=verdict[1]),
              Text(" · ".join(f"{n} {state.replace('_', ' ')}" for state, n in doc["totals"].items()))]
    if not doc["action_needed"]:
        lines.append(Text("Action needed: none", style="green"))
    head = Text("\n").join(lines)
    hosts = [d for w in doc["waves"] for d in w["hosts"]] + doc["left_out"]
    if host:
        hosts = [d for d in hosts if d["host"] == host] or sys.exit(f"{host} isn't in this report")
        return [head, *(x for d in hosts for x in host_tables(d))]
    # A letter per version, the targets first: the versions are long, the tables name them by letter
    before, after = doc["fleet"]["before"], doc["fleet"]["after"]
    versions = list(dict.fromkeys([*doc["targets"], *sorted(before, key=lambda v: -before[v]), *after]))
    letter = {v: chr(ord("A") + i) for i, v in enumerate(versions)}
    legend = block("Versions", ("", "Version", "Role", "Hosts before", "Hosts after"),
                   [(letter[v], v, "target" if v in doc["targets"] else "-", str(before.get(v, 0)),
                     str(after.get(v, 0))) for v in versions], expand=False)
    out = [head, block("Action needed", ACTION_ROW, [(a["host"], text(a["wave"]), a["state"], text(a["why"]),
                                                      text(a["next"])) for a in doc["action_needed"]]), legend]
    table = Table(*(Column(h, no_wrap=h != "Why", overflow="fold", ratio=1 if h == "Why" else None)
                    for h in WAVE_ROW), title="Hosts by wave", title_justify="left", title_style="bold", expand=True)
    for w in doc["waves"]:
        g = w["gate"]
        gate = f"{g['result']}: {g['failed']} of {g['tried']} failed" if "tried" in g else g["result"]
        for i, d in enumerate(w["hosts"]):
            change = letter.get(d["before"], "?") + ("" if d["state"] in ("untouched", "blocked", "skipped")
                                                     else f" → {letter.get(d['after'], '?')}")
            wave = Text.assemble((w["name"], "bold"), "\n", (gate, STYLE.get(g["result"], ""))) if i == 0 else ""
            table.add_row(wave, d["host"], Text(d["state"], style=STYLE.get(d["state"], "")), change,
                          took(d["seconds"]), text(d["why"]), end_section=i == len(w["hosts"]) - 1)
    out += [table if doc["waves"] else [],
            block("Left out of the plan", LEFT_OUT_ROW, [(d["host"], d["state"], letter.get(d["before"], "?"),
                                                         letter.get(d["target"], "?"), text(d["why"]))
                                                        for d in doc["left_out"]])]
    out += [x for d in hosts for x in host_tables(d)] if details else []
    return [x for x in out if x != []]


# One host's steps as a table, titled with who it is and how it ended
def host_tables(d) -> list:
    who = obj(d["identity"])
    title = " · ".join(filter(None, (d["wave"] or "left out", d["host"], d["state"], took(d["seconds"]),
                                     who.get("service_uuid") and f"service UUID {who['service_uuid']}",
                                     who.get("serial") and f"serial {who['serial']}")))
    caption = f"before {text(d['before'])} · target {text(d['target'])} · after {text(d['after'])}"
    return [block(title, STEP_ROW, [(s["step"], (s["started"] or "-")[11:19], took(s.get("seconds")),
                                     text(s.get("result")), step_detail(s)) for s in d["steps"]],
                  caption=caption, caption_justify="left") or Text(f"{title}: no steps recorded · {caption}")]


# The latest pipeline that did something (a dry run leaves no record): its plan and records, (None, the latest record)
# without any
def latest_pipeline() -> tuple[Path | None, list[Path]]:
    for d in sorted(RUNS.glob("pipeline-*"), reverse=True):
        plan, listed = d / "plan.json", d / "records.txt"
        if not plan.is_file():
            continue
        records = ([RUNS / Path(line).name for line in listed.read_text().split()] if listed.is_file()
                   else sorted(p for p in RUNS.glob("*.jsonl") if p.stat().st_mtime >= plan.stat().st_mtime))
        if records or (d / "report.json").is_file():
            return plan, records
    return None, sorted(RUNS.glob("*.jsonl"))[-1:]


# Report: a pipeline's report from its plan and records (the latest pipeline by default), or a run's from its record
def report(args) -> list:
    plan, records = (Path(args.plan), [Path(r) for r in args.record]) if args.plan or args.record \
        else latest_pipeline()
    missing = [str(p) for p in [*records, *([plan] if plan else [])] if not p.is_file()]
    if missing or not (plan or records):
        raise SystemExit(f"nothing to report: {', '.join(missing) or 'no pipeline or run record in ' + str(RUNS)}")
    name = plan.parent.name if plan else records[-1].stem
    doc = pipeline_report(json.loads(plan.read_text()) if plan else None, events_of(records), name, records)
    if args.save:
        Path(args.save).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return render(doc, details=args.details, host=args.host)


# --fault [HOST=]KIND as (host, kind); a kind alone, (*, kind), goes to every host
def fault_spec(value) -> tuple[str, str]:
    host, _, kind = value.rpartition("=")
    if kind not in FAULTS:
        raise argparse.ArgumentTypeError(f"{kind}: not one of {', '.join(FAULTS)}")
    return host or "*", kind


# Parse the command, run it, print what it returns and write the files asked for
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    c = argparse.ArgumentParser(add_help=False)  # step 1, shared by plan and run
    c.add_argument("--inventory", default=INVENTORY_FILE, help="the BMCs [<site>/inventory.yaml]")
    c.add_argument("--baseline", default=BASELINE, help="the approved versions [<site>/baseline.yaml]")
    c.add_argument("--images", default=IMAGES, help="the image catalog [<site>/images.yaml]")
    c.add_argument("--hosts", nargs="+", metavar="HOST", help="only these hosts of the inventory")
    c.add_argument("--canary", dest="canary_per_model", type=int, help="canary hosts per hardware model [policy]")
    c.add_argument("--waves", type=lambda v: [float(w) for w in v.split(",")], metavar="PCT,…",
                   help="cumulative %% of the other hosts done after each wave, e.g. 5,25,100 [policy]")
    c.add_argument("--max-per-rack", type=int, help="hosts of one rack in a wave, at most; 0 no limit [policy]")
    c.add_argument("--allow-downgrade", action="store_true",
                   help="also plan a downgrade, and an update whose direction can't be told")
    c.add_argument("--accept-no-rollback", action="store_true",
                   help="also plan a component with no backup image and no image of its running version")
    pl = commands.add_parser("plan", parents=[output_options(), c], help="step 1: pre-flight and waves, read-only")
    pl.add_argument("component", nargs="?", help='a row name of the firmware view and baseline.yaml, e.g. "Manager '
                                                 '(BMC)", or SoftwareId:<id>; without one, a report per host')
    pl.add_argument("--save", metavar="FILE", help="save the plan for run --plan, e.g. in a pipeline")
    pl.set_defaults(run=plan)
    r = commands.add_parser("run", parents=[output_options(), c], help="steps 1 to 6; a dry run without --yes")
    r.add_argument("component", nargs="?", help='a row name of the firmware view and baseline.yaml, or '
                                                'SoftwareId:<id>; with --plan, the plan\'s')
    r.add_argument("--plan", metavar="FILE", help="run a plan saved by plan --save: its component, files and waves")
    r.add_argument("--stage", default="all", choices=("all", "canary", "waves"),
                   help="with --plan: the canary, the waves after it, or all [all]")
    r.add_argument("--yes", action="store_true", help="really update the hosts that pass (only writable ones)")
    r.add_argument("--drain", help="command that empties a host, e.g. 'scontrol update NodeName={node} State=DRAIN "
                                   "Reason=firmware'; it runs only when the reset touches the host")
    r.add_argument("--undrain", help="command that gives a host back, e.g. 'scontrol update NodeName={node} "
                                     "State=RESUME'")
    r.add_argument("--halt-at", type=float, help="after the strict waves, stop when more than this share failed "
                                                 "[policy]")
    r.add_argument("--reset-timeout", type=int, default=900, help="seconds for a reset to come back [900]")
    r.add_argument("--fault", action="append", type=fault_spec, metavar="[HOST=]KIND",
                   help="lab testing: inject a fault into a host's update (every host's without HOST=); may repeat. "
                        + "; ".join(f"{kind}: {what}" for kind, what in FAULTS.items()))
    r.set_defaults(run=run)
    rp = commands.add_parser("report", parents=[output_options()],
                             help="a pipeline's report, from its plan and run records [the latest pipeline]")
    rp.add_argument("record", nargs="*", help="<site>/runs/<run id>.jsonl, several are merged")
    rp.add_argument("--plan", metavar="FILE", help="the pipeline's plan.json: its waves and the hosts it left out")
    rp.add_argument("--save", metavar="FILE", help=f"also write the report as JSON ({REPORT_SCHEMA}), for services")
    rp.add_argument("--details", action="store_true", help="every host's steps, with their evidence")
    rp.add_argument("--host", help="one host's steps only")
    rp.set_defaults(run=report)
    args = p.parse_args()
    logging.Formatter.converter = time.gmtime  # log times in UTC, like the records and the reports
    logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    show(args.run(args), args)
    sys.exit(getattr(args, "exit_code", 0))


if __name__ == "__main__":
    main()
