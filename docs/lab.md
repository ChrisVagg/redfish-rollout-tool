# The lab

Ten emulated OpenBMC BMCs, the image store and the image cache, on one Linux host: the only place the rollout has
written so far. This page covers how the lab is built, a firmware image's way from OpenBMC's builds to a BMC's
flash, how an update runs on these BMCs (checked against the Redfish standard), the bad-update scenarios, and every
Grafana panel over the lab. The rollout itself, the quick start and the tests are in the [README](../README.md);
the real fleet is in [Production](prod.md).

## The lab stack

![The lab](lab-stack.svg)

Each lab BMC is four layers in one Docker container ([lab/](../lab/)):

| Layer | What it is |
|---|---|
| Docker | [lab/Dockerfile](../lab/Dockerfile): Debian slim with QEMU and the flash image. [Compose](../lab/docker-compose.yaml) runs ten, `bmc1` to `bmc10` (about 2 GiB of RAM each), each with its own flash volume, so an update survives a restart. Healthy once `/redfish/v1` answers. |
| QEMU | OpenBMC's prebuilt `qemu-system-arm` with the `gb200nvl-bmc` machine: a full emulation of the BMC's own computer, the ASPEED AST2600 chip with its ARM cores, RAM, SPI flash and NIC. Only the BMC is emulated; there is no Grace CPU or Blackwell GPU behind it, so the lab updates the BMC's own firmware. |
| OpenBMC | The GB200 NVL build (`gb200nvl-obmc`) from OpenBMC's Jenkins: Linux, D-Bus and the phosphor services, booted from a 64 MiB flash image. |
| bmcweb | Redfish on the BMC's port 443. QEMU forwards it to the container, and Compose publishes it on `127.0.0.1:2441-2450` (SSH on `2221-2230`). Login `root` / `0penBmc`, OpenBMC's public default. |

### The firmware's way through the lab

The top lane of the drawing:

1. **OpenBMC builds a package.** Each Jenkins build of the GB200 NVL machine leaves one update package, a `.tar` with
   `image-bmc` (the whole 64 MiB flash), `MANIFEST` (its version), `publickey`, and an RSA-SHA256 signature for each.
2. **[lab/download-fw-images.sh](../lab/download-fw-images.sh) downloads the two newest** into `lab/staging/` (Jenkins
   keeps only the last three, so no build can be pinned). It does nothing else.
3. **[lab/promote-fw-images.sh](../lab/promote-fw-images.sh) promotes them**: it checks each package's signatures, uploads
   it into the store and deletes the download, so the store keeps the only copy. From the verified packages it writes
   the catalog, `lab/images.yaml` (each package's path in the store and its sha256), `lab/baseline.yaml`, which
   approves the newer build, so the first rollout is a real update, and `lab/bmc.mtd`, the older build's `image-bmc`.
4. **The first boot.** The BMC image carries `bmc.mtd`; each container copies it once into its own flash volume, and
   QEMU boots the BMC from that flash. The lab starts on the older build, and an update survives a restart.
5. **`make lab-up` runs it all in order**: the store first, both scripts when the catalog is missing, a check that
   the store holds every package the catalog names, then the BMCs, in batches.
6. **The rollout fetches and pushes.** Pre-flight downloads the package it needs from the cache into `lab/spool/`, the
   agent's own copy, and checks its sha256 against the catalog, before a byte of it reaches a BMC; the update pushes
   the whole package to the BMC's `MultipartHttpPushUri`.
7. **The BMC applies it.** OpenBMC's software manager on the BMC takes the package and applies `image-bmc` to the flash
   when the BMC resets, the rollout's reset step; QEMU then boots the new version. `make lab-reset` deletes only the
   BMCs' flash volumes, so they boot the older build again while the store keeps its packages, as a fleet reset never
   touches the approved store. `make lab-clean` deletes everything the lab made, store included, so the next
   `make lab-up` starts again from OpenBMC's newest builds.

### Booting ten BMCs

Ten emulated BMCs booting at once starve each other of CPU, so Compose boots them in batches of four, each once the
batch before is healthy; the first batch is the canary and wave 1. A BMC is healthy once its Manager reports `Enabled`.
Now and then a slow boot still runs a service before `/dev/mtd/u-boot-env` exists: systemd ends degraded, and bmcweb
reports the Manager `Quiesced`, health `Critical`. The health check then stops QEMU, and Docker's restart policy boots
the BMC again ([lab/Dockerfile](../lab/Dockerfile)); pre-flight would block it, as it would a real one.

### The image store and cache

Beside the BMCs, Compose runs the **image store** and the site's **image cache** in front of it, as production has
them. [lab/promote-fw-images.sh](../lab/promote-fw-images.sh) is the ingest gate: a package must hold its MANIFEST, its key
(`publickey`) and its image (`image-bmc`), each with an RSA-SHA256 signature, and every signature must verify against
the key pinned in the repository, [lab/openbmc-dev.pub](../lab/openbmc-dev.pub), never against the key the package
carries, which anyone could replace together with the image. A package that fails never reaches the store. The pinned
key is OpenBMC's development key, and its private half is public in OpenBMC's source tree: anyone can sign a modified
package with it. In the lab the gate shows how the verification works (required files, a pinned key, a refusal before
the store), and protects nothing. Production needs the vendor's signing key pinned, whose private half only the vendor
holds.

The store is SeaweedFS, an object store standing in for the production store (MinIO no longer publishes its images):
`promote-fw-images.sh` uploads the packages into its bucket `firmware`, which keeps the only copy and allows anonymous reads
only, so the lab holds no store credentials ([lab/store/](../lab/store/access.json)). The cache is nginx over HTTPS on `127.0.0.1:8443`, with a self-signed certificate `make lab-up` makes
([lab/cache/](../lab/cache/nginx.conf)): it fetches a package from the store once, keeps it and serves it from then on
(its `X-Cache-Status` header says `MISS` or `HIT`); nothing outside `/images/` is served. The rollout gets every
image from the cache, as a site agent does in production: pre-flight
downloads it into `lab/spool/` and checks it against the sha256 in `lab/images.yaml` (a copy that doesn't match is
deleted, to be fetched again), and the update pushes that copy. These BMCs can't pull an image themselves (their
UpdateService has no `SimpleUpdate`), so the lab runs the push half; pull, a URL handed to the BMC, is covered by
pre-flight's checks (the protocol and target the BMC allows) and the tests. With pull the agent never sees the bytes,
so nothing checks what the BMC downloads against the catalog's sha256 ([push and pull](prod.md#a-production-system---scaling-for-a-large-fleet)). The images are files: in the store's bucket, in the cache and in the agent's copy;
no database holds them, only the catalog's path and sha256 for each.

## How an update runs on the lab's BMCs

![The lab rollout: plan, canary and waves on the ten emulated BMCs, step by step](lab-rollout.svg)

An upload applied at the next reset, then the rollout's own reset, then a
version check. The rollout pushes the image to `MultipartHttpPushUri` with `@Redfish.OperationApplyTime: OnReset`
(about a minute for 64 MiB): the BMC keeps it for its next reset, and the Task it creates goes Running → Completed,
100 %, in 5 s, the BMC still up. The reset step then applies it, `Manager.Reset` `GracefulRestart` (the BMC is back
in about 4.5 minutes), and the post-check confirms the new version runs. While the image waits for the reset, these
BMCs don't show it: `SoftwareInventory` v1_1_0 has no `Active`, `Staged` (v1_12_0) or `Armed` (v1_15_0), so
pre-flight's "nothing staged" check can't see a waiting image here, and with no `Activate` action the reset is the only
way to apply it. `OnReset` rather than `Immediate`, which would
flash and reboot inside the task: the task's `Completed` is recorded before the BMC goes away, and the rollout chooses
the reset and times the return. bmcweb drops its tasks when it reboots, so with `Immediate` the task's end could not be
observed.

These BMCs have no JobService, so the Task carries every message of the update. Jobs are read, not driven: pre-flight
and the post-check read a BMC's Jobs as they read its Tasks (a failed one blocks, a new failed one fails the
post-check), but an apply scheduled as a Job, an `Activate` action, a staged image or a switch of A/B banks needs
support the rollout doesn't have yet. No BMC of this fleet exposes a Job: iDRAC 8 keeps its update jobs as Oem
`DellJob` resources, which a standard-only tool doesn't read.

### Checked against the standard, on a lab BMC

Each fact above was tried on bmc10 with requests built as
`rollout.py` builds them, against the latest DMTF schemas:
- **Apply times.** Its UpdateService (v1_11_1) advertises no `@Redfish.OperationApplyTimeSupport`, so each value
  was tried. `OnReset` gets 202 and a Task. `OnTargetReset`, `AtMaintenanceWindowStart`, `InMaintenanceWindowOnReset`
  and `OnStartUpdateRequest` get 400 `PropertyValueNotInList`.
- **A Completed `OnReset` update waits for the Manager's reset.** The BMC still ran the old version, the firmware
  inventory was empty meanwhile (no pending image), and the Manager reset (back in 215 s) booted the new one. No
  `ComputerSystem.Reset` was sent: the host isn't restarted.
- **A Task, not a Job.** The Task (v1_4_3, `TaskService/Tasks/0`) had no `Links.CreatedResources`, and
  `GET /redfish/v1/JobService` answers 404.
- **Nothing to cancel.** After a failed update (`TaskAborted`), the inventory was unchanged, and a reset booted the
  same version. Nothing was pending, so `SoftwareInventory.Cancel` (v1_14_0, *cancels the pending activation*) has
  nothing to act on; this BMC doesn't offer it anyway.

Grafana's **Lab · Update details** shows these facts, and per pipeline its evidence: the drain skipped, the push with
its `OnReset`, the Task, the Manager's reset, and each host's rollback, which the plan also shows
([Update details](#update-details)). The same update in production, resource by resource:
[Production](prod.md#the-update-resource-by-resource).

## The lab on the production system's layout

![The lab today, on the production system's layout](lab-solution-architecture.svg)

What the lab implements, drawn where [the production system](prod.md#a-production-system---scaling-for-a-large-fleet) has
each part; an empty place is a part it doesn't have yet. `lab/download-fw-images.sh` downloads OpenBMC's builds;
`lab/promote-fw-images.sh` checks their signatures, uploads them into the store, a SeaweedFS bucket that keeps the only
copy, records their sha256 and approves the newer. `make` is the CLI (`YES=1` approves; no API yet), `pipeline.sh`
and `rollout.py` the workflow engine, the YAML files the catalog and inventory, the run records the rollout state.
`rollout.py run` is the site agent: it fetches each image from the nginx cache, checks it and pushes it to ten emulated
BMCs over Redfish, then resets, post-checks and, on a failure, rolls back. It pushes every run's metrics to the
Pushgateway (`PUT /metrics/job/rollout`) and sends every event of its run record to Loki (`POST /loki/api/v1/push`);
exporter-lab reads every BMC over Redfish every 10 s. Prometheus scrapes `/metrics` from both, the Pushgateway every
5 s and exporter-lab every 10 s, and Grafana has Prometheus and Loki as its data sources.

## Bad updates

`make fault SCENARIO=<name>` runs the real pipeline to the build the lab doesn't run, with faults
injected where real ones would show (`rollout.py run --fault [HOST=]KIND`, lab only; [lab/scenario.sh](../lab/scenario.sh)).
Every scenario should stop the pipeline at a gate; the script checks how each host ended. The single-fault scenarios
hit every host, so the canary fails and the waves never run:

| Scenario | The bad update | What the rollout does | Canary ends |
|---|---|---|---|
| `silent-fail` | The BMC takes the image and reports `Completed`, but the firmware doesn't change: it got the running version's package as the target's | post-check ✗ → reinstall the version before → post-check ✓ | `rolled_back` |
| `unhealthy` | The new firmware flashes and boots, then fails the post-check (`--fault unhealthy`, where a health regression would show) | a real rollback: reflash the version before, check again | `rolled_back` |
| `rejected` | The BMC refuses the image (the payload is cut to 8 MiB on its way): its task ends in `Exception` | no reset, the old firmware still runs: nothing to roll back | `failed` |
| `bad-checksum` | The file doesn't match the catalog's sha256 | pre-flight blocks every host; the plan stage fails | `blocked` |
| `no-return` | The BMC isn't back within the reset timeout (45 s; a reset takes about 4.5 min) | the rollback reinstalls the version before, but its reset times out too: nothing verified, left for a person | `needs_attention` |

<details><summary>The report of <code>silent-fail</code></summary>

![The report of silent-fail](runs/silent-fail.svg)

</details>
<details><summary>The report of <code>unhealthy</code></summary>

![The report of unhealthy](runs/unhealthy.svg)

</details>
<details><summary>The report of <code>rejected</code></summary>

![The report of rejected](runs/rejected.svg)

</details>
<details><summary>The report of <code>bad-checksum</code></summary>

![The report of bad-checksum](runs/bad-checksum.svg)

</details>
<details><summary>The report of <code>no-return</code></summary>

![The report of no-return](runs/no-return.svg)

</details>

`hybrid` mixes good and bad updates in one rollout, the way a real one goes wrong:

| Wave | Hosts | Fault | Ends |
|---|---|---|---|
| canary | 2441 | none | `updated`, then soaked |
| wave 1 | 2442 | none | `updated` |
| wave 1 | 2443 | `unhealthy` | `rolled_back` |
| wave 1 | 2444 | `rejected` | `failed` |
| wave 2 | 2445 to 2450 | - | untouched: wave 1 is strict, and 2 of its 3 hosts failed |

The canary can't catch a fault that hits only some hosts; the halt after each wave is what stops it spreading. The
fleet is left on two builds, and the report says which host is where.

`make report` then shows the run: what needs a person, and each host's versions before and after.

After `rejected`, the aborted task stays in that BMC's `TaskService`: bmcweb keeps tasks until the BMC restarts and
only allows GET on them. Pre-flight then blocks the BMC (a failed task: a person has to look), so the next plan picks
another canary. `docker compose -f lab/docker-compose.yaml restart bmc1` clears it, as a BMC reset would.

## The lab in Grafana

Every panel of the Lab folder's four dashboards, section by section, over the ten emulated BMCs read every 10 seconds.
The first image of each dashboard is its top; the rest open below it. The pipelines are two real rollouts on the lab:
one where every gate passed, and one where the canary failed its post-check (the `unhealthy` fault), was rolled back,
and the gate halted the pipeline.

Five panels are empty in the lab, and say so: the fleet's readings nearest their thresholds and what is not OK, and
the host's thermal, power and system readings. QEMU emulates only the BMC, so there are no fans, temperatures or power
supplies behind it, and every component is OK; [the production screenshots](prod.md#the-production-fleet-in-grafana) show these panels full.

### Fleet manager

Every BMC at a glance: how many answer, the worst health, the shortest uptime and the longest crawl; a row per host
with its equipment, rack and health; and when each BMC answered Redfish, where every firmware update shows as its reset.

![Lab · Fleet manager: the counts, every host, and Redfish answering](grafana/lab/fleet.png)

<details><summary>Firmware: hosts per version, and per host with how Redfish can update it</summary>

![Lab · Fleet manager: firmware per version and per host](grafana/lab/fleet-firmware.png)

</details>

<details><summary>Readings nearest their upper critical threshold, and what is not OK: empty in the lab</summary>

![Lab · Fleet manager: readings nearest their thresholds, and what is not OK](grafana/lab/fleet-readings.png)

</details>

<details><summary>The BMCs' own memory, free storage and crawl times</summary>

![Lab · Fleet manager: the BMCs' memory, storage and crawl times](grafana/lab/fleet-1.png)

</details>

### Host

One BMC in full: what it is, whether it answers, its health and uptime; its firmware inventory with what Redfish can
update and how; the health of every component; then its readings by kind, and the BMC and its Redfish service.

![Lab · Host: one emulated BMC, its firmware and the health of every component](grafana/lab/host.png)

<details><summary>Chassis · Thermal and Chassis · Power: empty in the lab</summary>

![Lab · Host: thermal and power readings](grafana/lab/host-sensors.png)

</details>

<details><summary>Systems, empty in the lab, and Managers: the BMC's own CPU</summary>

![Lab · Host: system readings, and the BMC's CPU](grafana/lab/host-sensors-2.png)

</details>

<details><summary>The BMC and its Redfish service: memory, free storage, crawl time, failed GETs, answering</summary>

![Lab · Host: the BMC's memory, storage, crawl time, failed GETs and Redfish answering](grafana/lab/host-details-1.png)

</details>

### Pipeline

**Every gate passed.** The run (component, target, `OnReset`, elapsed), the verdict, the counts, the plan with each
host's pre-flight verdict, activation and rollback, the gate after each wave and each wave's progress.

![Lab · Pipeline: every gate passed](grafana/lab/pipeline-pass.png)

<details><summary>Every host step by step, the checks and recovery per step, and the decision log</summary>

![Lab · Pipeline: every host's numbered steps, the checks, and the decision log](grafana/lab/pipeline-pass-hosts.png)

</details>

<details><summary>One host's every event, as the run recorded it</summary>

![Lab · Pipeline: one host's every event](grafana/lab/pipeline-pass-host-events.png)

</details>

<details><summary>Timeline and timing: every host's step over time, Redfish answering, the longest steps, each reset</summary>

![Lab · Pipeline: every host's step over time, and the timing of each step](grafana/lab/pipeline-pass-timing.png)

</details>

**The canary rolled back.** It failed its post-check, the version before was reinstalled and checked, and the gate
halted the pipeline before wave 1: the other nine BMCs were never touched.

![Lab · Pipeline: the canary rolled back, the pipeline halted](grafana/lab/pipeline-rollback.png)

<details><summary>The canary step by step: the update, the failed post-check, the rollback, and the decision log</summary>

![Lab · Pipeline: the canary's steps, its rollback, and the decision log](grafana/lab/pipeline-rollback-hosts.png)

</details>

<details><summary>Every error, and the check after the flash that failed</summary>

![Lab · Pipeline: the errors, and the failed check](grafana/lab/pipeline-rollback-errors.png)

</details>

### Update details

How a BMC firmware update runs on the lab, each fact checked against the DMTF schemas and on a lab BMC, and the
rollback per resource; then a pipeline's own evidence, stage by stage: the plan's pre-flight of every host and each
check it made, step 1's pre-flight again, then the drain, the push, the Task, the reset and the rollback.

![Lab · Update details: four facts checked against Redfish, and the rollback per resource](grafana/lab/details.png)

<details><summary>The plan and step 1: every host's pre-flight, and each check against every host</summary>

![Lab · Update details: the plan's pre-flight of every host, and step 1's pre-flight again](grafana/lab/details-plan.png)

</details>

<details><summary>Steps 2 to 4 and the rollback: the drain, the push, the Task, the reset, each host's rollback</summary>

![Lab · Update details: the evidence of every host](grafana/lab/details-evidence.png)

</details>
