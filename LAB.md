# The lab

Ten emulated OpenBMC BMCs, the image store and the image cache, on one Linux host: the only place the rollout has
written so far, and where it is built and tested. This page has the detail behind the [README](README.md): how the
lab is built, how the rollout works and how an update runs on these BMCs, the policy, real runs and their reports,
the bad-update scenarios and the tests, the poller and the observability stack, and every Grafana panel over the
lab. The real fleet is in [PROD.md](PROD.md).

## The lab stack

![The lab](docs/lab-stack.svg)

Each lab BMC is four layers in one Docker container ([lab/](lab/)):

| Layer | What it is |
|---|---|
| Docker | [lab/Dockerfile](lab/Dockerfile): Debian slim with QEMU and the flash image. [Compose](lab/docker-compose.yaml) runs ten, `bmc1` to `bmc10` (about 2 GiB of RAM each), each with its own flash volume, so an update survives a restart. The container is ready once the Manager resource reports `Status.State: Enabled`; rollout health checks also read the System resource. |
| QEMU | OpenBMC's prebuilt `qemu-system-arm` with the `gb200nvl-bmc` machine: a full emulation of the BMC's own computer, the ASPEED AST2600 chip with its ARM cores, RAM, SPI flash and NIC. Only the BMC is emulated; there is no Grace CPU or Blackwell GPU behind it, so the lab updates the BMC's own firmware. |
| OpenBMC | The GB200 NVL build (`gb200nvl-obmc`) from OpenBMC's Jenkins: Linux, D-Bus and the phosphor services, booted from a 64 MiB flash image. |
| bmcweb | Redfish on the BMC's port 443. QEMU forwards it to the container, and Compose publishes it on `127.0.0.1:2441-2450` (SSH on `2221-2230`). Login `root` / `0penBmc`, OpenBMC's public default. |

### The firmware's way through the lab

The top lane of the drawing:

1. **OpenBMC builds a package.** Each Jenkins build of the GB200 NVL machine leaves one update package, a `.tar` with
   `image-bmc` (the whole 64 MiB flash), `MANIFEST` (its version), `publickey`, and an RSA-SHA256 signature for each.
2. **[lab/download-fw-images.sh](lab/download-fw-images.sh) downloads the two newest** into `lab/staging/` (Jenkins
   keeps only the last three, so no build can be pinned). It does nothing else.
3. **[lab/promote-fw-images.sh](lab/promote-fw-images.sh) promotes them**: it checks each package's signatures, uploads
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
batch before is ready; the first batch is the canary and wave 1. The container's readiness check passes once its
Manager reports `Status.State: Enabled`; the rollout checks resource health separately, as described below.
Now and then a slow boot still runs a service before `/dev/mtd/u-boot-env` exists: systemd ends degraded, and bmcweb
reports the Manager `Quiesced`, health `Critical`. The health check then stops QEMU, and Docker's restart policy boots
the BMC again ([lab/Dockerfile](lab/Dockerfile)); pre-flight would block it, as it would a real one.

### Availability and resource health

An HTTP 200 from `/redfish/v1` establishes that the Redfish service answers. To judge the equipment's health, read
the `ComputerSystem` resource under `/redfish/v1/Systems/` and the BMC's `Manager` resource under
`/redfish/v1/Managers/`: use `Status.HealthRollup`, falling back to `Status.Health`, and examine `Status.State`
alongside them. `Enabled` describes the resource's state; the health fields describe its condition.

The rollout already checks health on both resource types: pre-flight requires `OK` on each discovered System and
Manager, and post-check compares their health with the read before the update and fails if a previously seen resource
disappears. The container's Manager readiness check is only the first step; it does not establish System health.

Reading that health depends on the BMC and its Redfish service being available. The host's operating state is
separate: an independently powered BMC can remain available while the host is off or unhealthy, as
[Dell documents for iDRAC](https://www.dell.com/support/kbdoc/en-us/000179517/dell-poweredge-how-to-configure-the-idrac-system-management-options-on-servers).
This lab emulates the BMC only, so its System resource does not validate a real host's CPU, GPU or workload health.

### The image store and cache

Beside the BMCs, Compose runs the **image store** and the site's **image cache** in front of it, as production has
them. [lab/promote-fw-images.sh](lab/promote-fw-images.sh) is the ingest gate: a package must hold its MANIFEST, its key
(`publickey`) and its image (`image-bmc`), each with an RSA-SHA256 signature, and every signature must verify against
the key pinned in the repository, [lab/openbmc-dev.pub](lab/openbmc-dev.pub), never against the key the package
carries, which anyone could replace together with the image. A package that fails never reaches the store. The pinned
key is OpenBMC's development key, and its private half is public in OpenBMC's source tree: anyone can sign a modified
package with it. In the lab the gate shows how the verification works (required files, a pinned key, a refusal before
the store), and protects nothing. Production needs the vendor's signing key pinned, whose private half only the vendor
holds.

The store is SeaweedFS, an object store standing in for the production store (MinIO no longer publishes its images):
`promote-fw-images.sh` uploads the packages into its bucket `firmware`, which keeps the only copy and allows anonymous reads
only, so the lab holds no store credentials ([lab/store/](lab/store/access.json)). The cache is nginx over HTTPS on `127.0.0.1:8443`, with a self-signed certificate `make lab-up` makes
([lab/cache/](lab/cache/nginx.conf)): it fetches a package from the store once, keeps it and serves it from then on
(its `X-Cache-Status` header says `MISS` or `HIT`); nothing outside `/images/` is served. The rollout gets every
image from the cache, as a site agent does in production: pre-flight
downloads it into `lab/spool/` and checks it against the sha256 in `lab/images.yaml` (a copy that doesn't match is
deleted, to be fetched again), and the update pushes that copy. These BMCs can't pull an image themselves (their
UpdateService has no `SimpleUpdate`), so the lab runs the push half; pull, a URL handed to the BMC, is covered by
pre-flight's checks (the protocol and target the BMC allows) and the tests. With pull the agent never sees the bytes,
so nothing checks what the BMC downloads against the catalog's sha256 ([push and pull](PROD.md#a-production-system---scaling-for-a-large-fleet)). The images are files: in the store's bucket, in the cache and in the agent's copy;
no database holds them, only the catalog's path and sha256 for each.

## How the rollout works

![The lab rollout: plan, canary and waves on the ten emulated BMCs, step by step](docs/lab-rollout.svg)

Each site is a folder, `lab/` or `prod/`, picked with `SITE=lab` or `SITE=prod`. Three files describe its fleet:
`inventory.yaml` (the BMCs, how to reach them, their rack, whether they are `writable`), `baseline.yaml` (the approved
version per model and component) and `images.yaml` (the image file and sha256 per version).

[pipeline.sh](pipeline.sh) runs the canary first, then wave by wave, with a gate after each:

1. **plan**: reads every BMC live and pre-flights it, then puts the hosts that pass in waves as the site's rollout
   policy says ([Rollout policy](#rollout-policy)): a canary of each hardware model, then waves that grow, spread
   across racks. It saves `plan.json`, and the later stages run exactly that plan: it records the sha256 of every
   file it read (inventory, baseline, catalog, policy) and each BMC's identity, and a stage refuses to start when one
   of those files changed since.
2. **canary**: updates the canary hosts. Gate: any failure stops the pipeline, so wave 1 never starts.
3. **waves**: wave 1 starts only after the canary passed and soaked. After each wave, the gate: the first waves are
   strict like the canary (any failure stops the pipeline); later ones stop when more than `halt_at` of the hosts
   tried failed. Soak: after every wave but the last, when none of its hosts failed, the pipeline waits `soak`
   seconds, then runs the post-check again on each host the wave updated; one that fails now is `needs_attention`,
   and the gate halts.
4. **report**: one report of the whole pipeline, from the plan and the run records (see [Reports](#reports)). It
   runs even after a stop; a stopped pipeline exits 1.

Every host, in the canary and in each wave, goes through six steps; the hosts of one wave run side by side:

1. **pre-flight** again, on a fresh read: the host may have changed since the plan, and it must be the BMC the plan
   approved (the same identity: model, service UUID, serial).
2. **drain**, only when the reset restarts the host (BIOS, system firmware); a BMC reset leaves the host running. The
   drain command, from the site's `rollout.yaml`, must return only once the node is empty, its jobs ended; one that
   fails, or runs past 24 h, fails the host before any update.
3. **update**: multipart push of the image, or `SimpleUpdate` from a URL, applied on reset. The BMC runs it as a
   `Task` of its `TaskService`; every change of `TaskState`, `PercentComplete` and `Messages` is recorded, and the
   report shows the task, its messages and its progress under the step.
4. **reset**: `Manager.Reset` or `ComputerSystem.Reset`, graceful first, then wait until it answers again.
5. **post-check**: the target version runs, health is no worse than before (a system or manager that reported no
   health before and still doesn't passes; one that is gone fails), and no new job or task failed or hangs.
   When it fails, roll back: reinstall the version from before, reset, check again. A host that can't be rolled back
   stays drained for a person (`needs_attention`).
6. **undrain**: the host goes back to the scheduler after an update, a rollback, or a failure before the reset; only a
   host left for a person stays drained. An undrain that fails leaves the host `needs_attention`: its firmware may be
   fine, but it isn't back in service.

Pre-flight gives each host **go**, **skip** (nothing to do, or busy: try a later wave) or **block** (a person has to
look), with every reason. It blocks a downgrade or a version order it can't tell (unless `--allow-downgrade`), a
component Redfish can't update (`Updateable` false, `WriteProtected`), a target below `LowestSupportedVersion`, a
missing image, a sha256 mismatch, an image over `MaxImageSizeBytes`, no rollback path (no image of the running version
to reinstall, unless `--accept-no-rollback`; an A/B bank doesn't count, as switching banks isn't built), a reset that
restarts the host with no drain configured, a disabled update service, health not OK, and failed jobs.

Every step goes into `<site>/runs/<run id>.jsonl`, its intent before it and its result after, so a run stopped halfway
still shows where each host was. The records explain what happened; they don't resume a run or lock a host: a stopped
pipeline is planned and run again, and pre-flight skips the hosts already on the target ([Not built yet](PROD.md#not-built-yet)). Each pipeline keeps its plan and reports in `<site>/runs/pipeline-<time>/`.

`make dry-run` makes no firmware changes. It still reads the BMCs, saves the plan and the stages' HTML files, and
publishes the plan and its pre-flight checks to Loki when `LOKI` is set (the lab Makefile sets it). A successful dry
run has no per-host execution records and does not produce the combined `report.json` or `report.html`.

## How an update runs on the lab's BMCs

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
[Production](PROD.md#the-update-resource-by-resource).

## Rollout policy

Each site has a `rollout.yaml` next to its inventory and baseline. `plan` reads it, an option overrides one value for
one plan (`--canary`, `--waves`, `--max-per-rack`, `--halt-at`), and the plan and the report record what was used. The
pipeline's stages run the plan's policy, and refuse a change to it (`--halt-at` with a saved plan, or a `rollout.yaml`
edited since the plan). It is reviewed like code: it decides how many hosts a bad image can reach before something stops it.

| Setting | [lab](lab/rollout.yaml) | [production](prod/rollout.yaml) | What it does |
|---|---|---|---|
| `canary_per_model` | 1 | 1 | Hosts of each hardware model that go first. A bad image is almost always model-specific, so one canary for the whole fleet proves nothing for the other models. Hosts marked `canary: true` in the inventory go first, and the canaries keep to `max_per_rack` too, unless every host of a model is in a full rack. |
| `waves` | 33, 100 | 5, 25, 100 | Cumulative % of the other hosts done after each wave: small while the evidence is thin, bigger once gates have passed. |
| `max_per_rack` | 2 | 1 | Hosts of one rack in the same wave, at most: a rack never loses more nodes than it can spare. The rest wait for a later wave, so it also caps a wave's size: 1,000 hosts in 100 racks at 1 per rack take 11 waves after the canary (50, then up to 100 each); at 2 per rack, 6. Racks only: limits per PDU, fabric pod or capacity aren't built. |
| `strict_waves` | 1 | 1 | The first waves after the canary that halt on any failure, as the canary does. |
| `halt_at` | 10% | 2% | Later waves stop when more than this share of the hosts tried failed. 10% of 1,000 would be 100 broken BMCs. |
| `max_parallel` | 10 | 50 | Updates running at once within a wave: the BMCs and the image server set the limit. |
| `soak` | 60 s | 30 min | After every wave but the last, when none of its hosts failed: wait, then run the post-check again on each host it updated; one that fails now is `needs_attention` and halts the pipeline. Some faults show only after a while. |
| `drain`, `undrain` | none configured | none configured | Optional scheduler commands, run only for a reset that restarts the host; pre-flight blocks such an update without a drain. A drain must return only once the node is empty, its jobs ended; a drain past 24 h fails the host. The hooks have unit tests, but no integration with a real scheduler has been validated here. |

With the lab's 10 BMCs in 3 racks: canary 2441 (fw image), wave 1 of 3 (one per rack), wave 2 of 6 (two per rack).

## What a run looks like

Every image here is a real run on the lab: its report, as `rollout.py report --svg` draws it from the pipeline's plan
and run records.

**An update of all ten BMCs**: the canary, then waves of 3 and 6, every gate passed.

![The report of an update of all ten BMCs](docs/runs/update.svg)

The terminal while it ran: every stage, and every step of every BMC as it happened

![The terminal of make update](docs/runs/update-live.svg)

**A rollout that goes wrong** (`hybrid`): wave 1 has a good host, one whose new firmware fails its post-check and is
rolled back, and one whose BMC refuses the image. Wave 1 is strict, so the pipeline stops there and wave 2 never
starts. The other bad updates, one fault each, are under [Bad updates](#bad-updates).

![The report of the hybrid scenario](docs/runs/hybrid.svg)

## Reports

Every pipeline that changed something, or stopped, leaves one report in `<site>/runs/pipeline-<time>/`, built from
its plan and its run records. It comes in two forms, drawn from the same data so they can't disagree:

- **`report.json`**, for services (schema `rollout-report/1`). Top level: `result` (`passed`, `halted`, `blocked`,
  `incomplete`, `nothing to do`), `stopped` (the wave, `failed` of `tried`, the limit, the waves not run), `totals`
  per host state, `fleet` (hosts per version, `before` and `after`), `action_needed`, `waves` and `left_out`
  (hosts the plan didn't take, with why). Each wave has its `gate` (`passed`, `halted`, `not run`) and its hosts;
  each host its `state`, `before`, `target` and `after` versions, `identity` (the Redfish service's `UUID`, a
  serial number where reported), `why`, `next` and its `steps`: the pre-flight `checks` (every check made, each
  `{check, ok, detail}`), the update's `method` (`push` to `MultipartHttpPushUri`, or `pull` with
  `SimpleUpdate`), `uri`, `image` (file and sha256) and `task` (URI, `TaskState`, `TaskStatus`, `PercentComplete`,
  every message by `MessageId`) with its `progress`, the reset's `action`, `reset_type` and `resource`, the
  post-check's `checks`, and a rollback's `reason`, `to` and `verified`. Times are UTC ISO 8601, durations in
  seconds, states are fixed words; the text is only ever next to them.
- **The report for people**, in the terminal and as `report.html`: the verdict first, then what needs a person
  (host, why, next step), the versions (a letter each, with hosts before and after), and every host by wave with
  the wave's gate. `report.html` adds every host's steps with their evidence. `make report` shows the latest
  pipeline's; `HOST=127.0.0.1:2443` shows one host's steps.

One host's steps with their evidence: the canary of `unhealthy`

![One host's steps: every pre-flight check, the push and its task, the reset, the post-check that failed, the rollback](docs/runs/unhealthy-host.svg)

## Bad updates

`make fault SCENARIO=<name>` runs the real pipeline to the build the lab doesn't run, with faults
injected where real ones would show (`rollout.py run --fault [HOST=]KIND`, lab only; [lab/scenario.sh](lab/scenario.sh)).
Every scenario should stop the pipeline at a gate; the script checks how each host ended. The single-fault scenarios
hit every host, so the canary fails and the waves never run:

| Scenario | The bad update | What the rollout does | Canary ends |
|---|---|---|---|
| `silent-fail` | The BMC takes the image and reports `Completed`, but the firmware doesn't change: it got the running version's package as the target's | post-check ✗ → reinstall the version before → post-check ✓ | `rolled_back` |
| `unhealthy` | The new firmware flashes and boots, then fails the post-check (`--fault unhealthy`, where a health regression would show) | a real rollback: reflash the version before, check again | `rolled_back` |
| `rejected` | The BMC refuses the image (the payload is cut to 8 MiB on its way): its task ends in `Exception` | no reset, the old firmware still runs: nothing to roll back | `failed` |
| `bad-checksum` | The file doesn't match the catalog's sha256 | pre-flight blocks every host; the plan stage fails | `blocked` |
| `no-return` | The BMC isn't back within the reset timeout (45 s; a reset takes about 4.5 min) | the rollback reinstalls the version before, but its reset times out too: nothing verified, left for a person | `needs_attention` |

The report of `silent-fail`

![The report of silent-fail](docs/runs/silent-fail.svg)

The report of `unhealthy`

![The report of unhealthy](docs/runs/unhealthy.svg)

The report of `rejected`

![The report of rejected](docs/runs/rejected.svg)

The report of `bad-checksum`

![The report of bad-checksum](docs/runs/bad-checksum.svg)

The report of `no-return`

![The report of no-return](docs/runs/no-return.svg)

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

## Test-driven development

`make test` runs [test_rollout.py](test_rollout.py) in about a second, with no BMC. It tests the decisions that keep a
bad image from spreading, on real Redfish data: the lab BMC as a collect saved it
([fixtures/openbmc-gb200.json](fixtures/openbmc-gb200.json)), with one fact changed per case.

| What decides | The cases |
|---|---|
| Pre-flight (`check`) | go; a skip when already on the baseline or nothing is approved for the model; a downgrade, or an order nobody can tell, blocked unless `--allow-downgrade` (dotted integers compare as numbers: 2.9 to 2.10 is up); a missing image, a sha256 that doesn't match, an image over `MaxImageSizeBytes`; no image to reinstall blocked unless `--accept-no-rollback`, even with an A/B bank reported; a reset that restarts the host blocked without a drain; an unreachable BMC, `Updateable` false, a Critical manager or a failed task blocked, a running task skipped |
| Post-check (`after_checks`) | the target runs; health no worse than before; a manager gone since fails; no new failed task. A task that says `Completed` while the old firmware still runs fails it |
| One host (`run_host`, `scheduler`) | a failure before the reset: the old firmware still runs, nothing to roll back; after it: the version that ran before is reinstalled, not the target; a failed rollback, or none possible: `needs_attention`; a drain that fails or runs past its timeout fails the host before any update; an undrain that fails: `needs_attention`; a BMC that isn't the one the plan approved is blocked |
| The frozen plan (`changed_inputs`) | an input file changed or gone since the plan stops the run |
| The gate (`gate`) | any failure halts the canary and the strict waves; a later wave halts over `halt_at`, exactly `halt_at` passes; a host that failed its soak halts any wave; blocked and skipped hosts were never tried |
| The ingest gate (`lab/promote-fw-images.sh`) | a package signed with the pinned key is uploaded and written into the catalog; one without its image's signature, with its image changed after signing, without a required file, signed with another key, or not signed at all, is refused, and nothing reaches the store |
| The image cache (`image_problems`) | an image fetched from a real HTTP server into the agent's copy and checked; one the cache doesn't have, or bytes that don't match, block, and the bad copy is deleted |
| The plan, update methods, metrics, the exporter | waves per model and rack; canaries keep to the rack limit; push or pull against what the BMC allows; metrics mid-run; one series per sensor |

Only the network is replaced, in `run_host`: the read and the update, which need a BMC. Everything that decides runs as
it does in a rollout.

A test is worth something only if it fails when the code is wrong. The gate and the post-check's decision were written
test first: the tests failed (there was no `gate` or `after_checks`), then the code moved out of `run` and
`post_check` until they passed. For the code that came before its tests, [mutations.py](mutations.py) (`make mutations`)
breaks one rule of `rollout.py` at a time the way a careless edit would: a downgrade let through,
versions compared as text, a sha256 mismatch ignored, the rollback installing the target, the gate halting at exactly
`halt_at`, an image never fetched from the cache, an A/B bank counted as a way back, a host reset with no drain, a
changed plan run anyway, and 18 more. For each break, the test that guards the rule must fail: 27 of 27 do. Its first run found a
gap: the strict-wave case had enough failures to halt anyway, so it passed with wave 1 not strict; the case now has 1
failure in 11 hosts.

The two run at different times. `make test` is the gate: it runs on every change, and `make update` runs it first, so
no real update starts on broken decision logic (the gate is local: no CI runners). `make mutations` takes a few seconds
and runs before a change to a safety rule: it finds each rule by its line, so a rule rewritten there must be rewritten
in its list too, and that would make a poor gate.

The unit tests stop at the network. Beyond it, `make fault SCENARIO=…` runs the real pipeline on the lab's BMCs with
real faults ([Bad updates](#bad-updates)): the push, the task, the reset, the wait. A change starts the same way: a
test that fails, then the code that makes it pass.

## The poller

[poller.py](poller.py) polls the resources each BMC's Redfish service exposes, from `/redfish/v1` down, and never
writes. `make collect` crawls every BMC of the site into `<site>/snapshots/`; the views read the last collect without
touching the BMCs:

| Target | What it shows |
|---|---|
| `make health` | health counts per host, then everything not OK, jobs to check and failed requests |
| `make firmware` | baseline compliance per host, then firmware per model against the baseline |
| `make inventory` | one row per host: identity, CPUs, GPUs, memory, drives, NICs, PSUs |
| `make telemetry` | every sensor of every host with its thresholds; the hottest and highest power per host |
| `make capabilities` | what each hardware model can do: Redfish version, services, actions, update methods |
| `make diff` | what changed since the previous collect |

`make detail HOST=…` shows everything about one host, and `make views` writes every view into `<site>/reports/` as
HTML, JSON and CSV. Standard Redfish properties only, read the same way on every vendor. Run without stopping, the
same crawl is the exporters of [Observability](#observability), which serve each read as Prometheus metrics.

## Observability

`make monitor-up` starts the Docker services of [observability/](observability/docker-compose.yaml), every one on
127.0.0.1 only:

| Service | What it does |
|---|---|
| `exporter-lab`, `exporter-prod` | `poller.py exporter`: crawls every BMC of its site, each on its own loop, and serves the latest read on `/metrics`: what the equipment is (`redfish_host_info`: the inventory's vendor, project and rack, the BMC's Manufacturer and Model), whether it answered (`redfish_up`) and how many of its GETs failed (`redfish_failed_requests`: what they would have read is missing from that read), every reading with its unit (`redfish_reading`), its thresholds (`redfish_reading_threshold`) and how far past them it is (`redfish_reading_crossed`: 0 within, 1 caution, 2 critical, 3 fatal, upper or lower), the health of every object with a `Status`, each fan and power supply too (`redfish_health`), each firmware component with its running version, whether Redfish can update it and how an image gets in, push or pull, the same checks as `rollout.py plan` (`redfish_firmware_info`), the BMC's last reset (`LastResetTime`), and its memory and free storage (`ManagerDiagnosticData`). Standard properties only, read as `collect` reads them. The lab every 10 s on `:9101`; prod every 5 min on `:9102`, read-only, with `prod/.env`'s accounts; without a `prod/inventory.yaml` (a fresh clone) it reads nothing and stays up. |
| Pushgateway `:9091` | Where `rollout.py run` pushes its report as metrics after every recorded event (`PUSHGATEWAY`): hosts by wave and state, versions, every step's duration, every check after the flash, each gate. Best effort: if it is down, the rollout logs it once and goes on. It keeps every pipeline until one is deleted, and the Pipeline dashboard lists the ones it holds, newest first: `curl -X DELETE http://127.0.0.1:9091/metrics/job/rollout/site/lab/pipeline/<pipeline>/stage/<canary or waves>` drops one. |
| Loki `:3100` | Where `rollout.py run` sends every event of its run record as it writes it (`LOKI`): each step of each host with its detail, the Task's messages, the checks. Labelled only `job` and `site`; the pipeline, stage, wave, host and step are fields of the JSON line, which LogQL's `json` reads, so it stays one stream per site however many hosts and pipelines there are. Keeps 15 days, best effort like the Pushgateway. |
| Prometheus `:9090` | Scrapes both, labels each exporter's metrics with its `site`, keeps 15 days. |
| Grafana `:3000` | A **Lab** and a **Prod** folder, the same three dashboards in each, fixed to its site. **Fleet manager**, every BMC: BMCs answering, worst health, readings past critical, a row per host with its equipment (vendor, manufacturer, model, project, rack) and failed GETs that opens its own dashboard, Redfish up over time (a firmware update shows as the reset's gap), firmware by hardware model and version (two versions of one component on one model is drift) and by host with Updateable, Push and Pull, the readings nearest their upper critical, everything not OK, the BMCs' own memory and storage. **Host**, one BMC: what it is, its firmware inventory and what Redfish can update and how, the health of every component, worst first, then a row each for Chassis · Thermal, Chassis · Power, Systems and Managers (the BMC itself): a chart per kind of reading, in its unit, with min, max, mean and last in the legend and each reading's caution and critical thresholds as dashed lines (orange, red). **Pipeline**, per pipeline, from both Prometheus and Loki, laid out as an operations page: the run (site, component, target, `OperationApplyTime`, elapsed, how fresh the last event is); the verdict, PASSED, RUNNING or HALTED, with how the latest stage ended in words; five counts (updated and verified, rolled back and failed, needing a person and blocked, running and not started, still drained); the plan, first as in a pipeline (the policy it used, and every host's pre-flight verdict with its wave, running version, target, its update's activation, whether the host restarts, and the rollback the rollout would carry out), beside the gate after the canary and each wave and each wave's progress; the host evidence, one row per host with its wave, its versions before and after, and each step's outcome, numbered as a host goes through them (1 pre-flight, 2 drain, 3 update with its Task message, 4 reset with its seconds, 5 post-check and a rollback, 6 undrain, then the soak and the outcome), each host linking to its every event and its own dashboard; checks and recovery counted per numbered step beside the decision log, the plan saved first. Below: one host's every event, every host's step on a timeline, the BMCs answering Redfish, the longest steps and each reset's time, every error and every failed check. The gates are annotations on its graphs. **Update details** (the lab only): how a BMC firmware update runs here, four facts checked against the DMTF schemas and on a lab BMC (`OnReset`, the Manager's reset alone, a Task and no Job, nothing staged to cancel), the rollback per resource the rollout updates, and per pipeline, stage by stage: the plan (every host's pre-flight verdict, and each of its checks against every host, ✓ or ✗ with why), step 1's pre-flight again on a fresh read with the BMC's identity and its checks, then the drain, the push, the Task, the reset and each host's rollback. |

Every panel, as screenshots: [the lab](#the-lab-in-grafana) and [the production fleet](PROD.md#the-production-fleet-in-grafana).

**Two stores, two ways in.** Prometheus pulls: every 10 s it fetches `/metrics` from each site's exporter, and every
5 s from the Pushgateway, and the exporter itself polls each BMC over Redfish, so what changes between two reads goes
unseen. Prometheus keeps numbers over time, labelled (hosts per state, seconds per step, every sensor reading), for
graphs, counts and thresholds. Loki keeps events with their detail, for what happened to one host and why.

### How the lab sends events to Loki

`rollout.py` generates the rollout events: pre-flight results, update progress, polled Task states and messages,
resets, post-checks and rollbacks. During execution it writes each event to the local JSONL run record, then
`push_event` sends it to `POST /loki/api/v1/push` when `LOKI` is set. The saved plan and its pre-flight checks are
also sent, including during a dry run. The lab Makefile sets `LOKI=http://127.0.0.1:3100`.

The monitoring flow is `rollout.py → Loki → Grafana`; the JSONL file is the local execution record. Loki receives
HTTP pushes from the rollout; it does not subscribe to the BMC. This works without Redfish `EventService`, because
the rollout creates these events from its actions and the Redfish responses it reads. Delivery is best effort:
after a failed push, the sender logs the failure and stops forwarding for that invocation; execution continues and
its JSONL record remains local.

BMC-originated events, such as independent hardware alerts, are a separate source. They are not collected into Loki
by this repository in either site. The lab's recorded attempt to open bmcweb's advertised server-sent event stream
returned 404; that observation alone does not establish support for other event delivery methods. A production
event receiver and Redfish subscriptions are [future work](PROD.md#future-work-bmc-events-into-loki).

Grafana is admin only: no anonymous access, the one account from `observability/grafana/.grafana.env`, which
[grafana.ini](observability/grafana/grafana.ini) reads. Grafana creates it on its first start only; after that, change
its password in the UI. The admin can change and save anything in the UI, and it stays in Grafana's volume across
`make monitor-down` and up, except the six dashboards: every Grafana start loads them again from
[observability/grafana/dashboards/](observability/grafana/dashboards/). To keep a change to one, export it over its
lab file (Export → Export as JSON), or save it as a copy. Only the lab dashboards are kept in git: `make monitor-up`
makes the prod ones from them ([prod_dashboards.py](observability/grafana/prod_dashboards.py): the same panels fixed
to site prod, and a read-only note on the pipeline), so a change is made once and both folders get it.

## The lab in Grafana

Every panel of the Lab folder's four dashboards, section by section, over the ten emulated BMCs read every 10 seconds.
The first image of each dashboard is its top; the rest follow it down the page. The pipelines are real rollouts on the lab:
one where every gate passed, then the [bad updates](#bad-updates), each stopped at its gate.

Five panels are empty in the lab and not shown here: the fleet's readings nearest their thresholds and what is not OK,
and the host's thermal, power and system readings. QEMU emulates only the BMC, so there are no fans, temperatures or power
supplies behind it, and every component is OK; [the production screenshots](PROD.md#the-production-fleet-in-grafana) show these panels full.

### Fleet manager

Every BMC at a glance: how many answer, the worst health, the shortest uptime and the longest crawl; a row per host
with its equipment, rack and health; and when each BMC answered Redfish, where every firmware update shows as its reset.

![Lab · Fleet manager: the counts, every host, and Redfish answering](docs/grafana/lab/fleet.png)

Firmware: hosts per version, and per host with how Redfish can update it

![Lab · Fleet manager: firmware per version and per host](docs/grafana/lab/fleet-firmware.png)

The BMCs' own memory, free storage and crawl times

![Lab · Fleet manager: the BMCs' memory, storage and crawl times](docs/grafana/lab/fleet-1.png)

**Damaged hosts.** The fleet during and after the [bad updates](#bad-updates). During `no-return`, its canary (2442)
isn't back after its reset: one BMC not answering, its row `down`, its gap in Redfish answering. The earlier gaps on
2441 are the resets of `silent-fail`'s update and rollback.

![Lab · Fleet manager: 2442 not answering during no-return](docs/grafana/lab/fleet-down.png)

After `hybrid`, every BMC answers and is OK, but the fleet is on two builds: two Manager (BMC) versions on one model,
2441 and 2442 on 1367 and the rest on 1375. The gaps are the hour's resets: `silent-fail`'s two on 2441, then a reset
of 2441 to clear `rejected`'s Task, `no-return`'s two on 2442, then `hybrid`'s: 2441, 2442, and 2443's update and
rollback.

![Lab · Fleet manager: after hybrid, two Manager (BMC) versions on one model](docs/grafana/lab/fleet-drift.png)

### Host

One BMC in full: what it is, whether it answers, its health and uptime; its firmware inventory with what Redfish can
update and how; the health of every component; then its readings by kind, and the BMC and its Redfish service.

![Lab · Host: one emulated BMC, its firmware and the health of every component](docs/grafana/lab/host.png)

Managers: the BMC's own CPU

![Lab · Host: the BMC's CPU](docs/grafana/lab/host-sensors-2.png)

The BMC and its Redfish service: memory, free storage, crawl time, failed GETs, answering

![Lab · Host: the BMC's memory, storage, crawl time, failed GETs and Redfish answering](docs/grafana/lab/host-details-1.png)

### Pipeline

**Every gate passed.** The run (component, target, `OnReset`, elapsed), the verdict, the counts, the plan with each
host's pre-flight verdict, activation and rollback, the gate after each wave and each wave's progress.

![Lab · Pipeline: every gate passed](docs/grafana/lab/pipeline-pass.png)

Every host step by step, the checks and recovery per step, and the decision log

![Lab · Pipeline: every host's numbered steps, the checks, and the decision log](docs/grafana/lab/pipeline-pass-hosts.png)

One host's every event, as the run recorded it

![Lab · Pipeline: one host's every event](docs/grafana/lab/pipeline-pass-host-events.png)

Timeline and timing: every host's step over time, Redfish answering, the longest steps, each reset

![Lab · Pipeline: every host's step over time, and the timing of each step](docs/grafana/lab/pipeline-pass-timing.png)

**`unhealthy`: the canary rolled back.** It failed its post-check, the version before was reinstalled and checked, and
the gate halted the pipeline before wave 1: the other nine BMCs were never touched.

![Lab · Pipeline: the canary rolled back, the pipeline halted](docs/grafana/lab/pipeline-rollback.png)

The canary step by step: the update, the failed post-check, the rollback, and the decision log

![Lab · Pipeline: the canary's steps, its rollback, and the decision log](docs/grafana/lab/pipeline-rollback-hosts.png)

Every error, and the check after the flash that failed

![Lab · Pipeline: the errors, and the failed check](docs/grafana/lab/pipeline-rollback-errors.png)

**`silent-fail`: the update changed nothing.** The canary's Task ended `Completed` and the BMC came back from its
reset, but still on the version before: the post-check's running-version check failed, the rollback reinstalled it,
and the gate halted.

![Lab · Pipeline: silent-fail, the canary's steps, its rollback, and the decision log](docs/grafana/lab/pipeline-silent-fail-hosts.png)

The errors, and the check that caught it: the running version

![Lab · Pipeline: silent-fail, the errors and the failed running-version check](docs/grafana/lab/pipeline-silent-fail-errors.png)

**`rejected`: the BMC refused the image.** Its Task ended in `Exception` (`TaskAborted`) before any reset, so the old
firmware still runs: the canary is `failed`, with nothing to roll back.

![Lab · Pipeline: rejected, the canary's Task ended in Exception](docs/grafana/lab/pipeline-rejected-hosts.png)

The errors: the Task's `Exception`, the host failed, the gate halted

![Lab · Pipeline: rejected, the errors](docs/grafana/lab/pipeline-rejected-errors.png)

**`no-return`: the BMC didn't come back.** Pre-flight blocked 2441 first: `rejected` had left its aborted Task there
(see [Bad updates](#bad-updates)), so the plan took 2442 as the canary. 2442 wasn't back 45 s after its reset; the
rollback reinstalled the version before, and its reset timed out too. Nothing is verified: `needs_attention`, for a
person.

![Lab · Pipeline: no-return, 2441 blocked by its failed Task, the canary needs a person](docs/grafana/lab/pipeline-no-return.png)

The canary step by step: the update, the reset that timed out, the rollback whose reset timed out too

![Lab · Pipeline: no-return, the canary's steps and the decision log](docs/grafana/lab/pipeline-no-return-hosts.png)

The canary over time, and when it answered Redfish: down after each reset

![Lab · Pipeline: no-return, the canary's steps over time and Redfish answering](docs/grafana/lab/pipeline-no-return-timing.png)

The errors: each reset that timed out

![Lab · Pipeline: no-return, the errors](docs/grafana/lab/pipeline-no-return-errors.png)

**`hybrid`: good and bad updates in one rollout.** The canary passed its soak; in wave 1, 2442 updated, 2443 failed
its post-check and was rolled back, 2444 refused the image. 2 of 3 failed, over the 10 % halt: wave 2 never started.

![Lab · Pipeline: hybrid, halted after wave 1](docs/grafana/lab/pipeline-hybrid.png)

Every host step by step, and the decision log

![Lab · Pipeline: hybrid, every host's steps and the decision log](docs/grafana/lab/pipeline-hybrid-hosts.png)

Every host over time: the canary, then wave 1, then the halt

![Lab · Pipeline: hybrid, every host's step over time, and the timing of each step](docs/grafana/lab/pipeline-hybrid-timing.png)

`bad-checksum` isn't here: pre-flight blocks every host, so no stage runs and nothing reaches the Pushgateway, where
the dashboard lists its pipelines. [Its report](#bad-updates) shows it.

### Update details

How a BMC firmware update runs on the lab, each fact checked against the DMTF schemas and on a lab BMC, and the
rollback per resource; then a pipeline's own evidence, stage by stage: the plan's pre-flight of every host and each
check it made, step 1's pre-flight again, then the drain, the push, the Task, the reset and the rollback.

![Lab · Update details: four facts checked against Redfish, and the rollback per resource](docs/grafana/lab/details.png)

The plan and step 1: every host's pre-flight, and each check against every host

![Lab · Update details: the plan's pre-flight of every host, and step 1's pre-flight again](docs/grafana/lab/details-plan.png)

Steps 2 to 4 and the rollback: the drain, the push, the Task, the reset, each host's rollback

![Lab · Update details: the evidence of every host](docs/grafana/lab/details-evidence.png)

## The lab on the production system's layout

![The lab today, on the production system's layout](docs/lab-solution-architecture.svg)

What the lab implements, drawn where [the production system](PROD.md#a-production-system---scaling-for-a-large-fleet) has
each part; an empty place is a part it doesn't have yet. `lab/download-fw-images.sh` downloads OpenBMC's builds;
`lab/promote-fw-images.sh` checks their signatures, uploads them into the store, a SeaweedFS bucket that keeps the only
copy, records their sha256 and approves the newer. `make` is the CLI (`YES=1` approves; no API yet), `pipeline.sh`
and `rollout.py` the workflow engine, the YAML files the catalog and inventory, the run records the rollout state.
`rollout.py run` is the site agent: it fetches each image from the nginx cache, checks it and pushes it to ten emulated
BMCs over Redfish, then resets, post-checks and, on a failure, rolls back. It pushes every run's metrics to the
Pushgateway (`PUT /metrics/job/rollout`) and sends every event of its run record to Loki (`POST /loki/api/v1/push`);
exporter-lab reads every BMC over Redfish every 10 s. Prometheus scrapes `/metrics` from both, the Pushgateway every
5 s and exporter-lab every 10 s, and Grafana has Prometheus and Loki as its data sources.
