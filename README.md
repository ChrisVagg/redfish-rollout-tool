# Redfish firmware rollout

Automated BMC firmware updates for a server fleet over the standard DMTF [Redfish](https://www.dmtf.org/standards/redfish)
API: pre-flight checks, a canary per hardware model, growing waves behind gates, a post-update health check and an
automatic rollback. Standard Redfish only, no vendor Oem code: one code path for every vendor.

| Site | What it is | What has run there |
|---|---|---|
| **Lab** | 10 emulated OpenBMC BMCs (GB200 NVL build, QEMU in Docker), a signature-checking image ingest, an image store and an HTTPS image cache | every update, rollback and fault scenario |
| **Production** | ASUS, Dell, HPE, Gigabyte and Supermicro servers, 7 hardware models | read-only: inventory, telemetry, BMC logs, plans, dry runs |

Writes have three gates: `make update` refuses `SITE=prod`, `rollout.py run` needs `--yes`, and it only touches hosts
tagged `writable` in NetBox. No production host is tagged.

![The rollout: plan, canary and waves on the ten lab BMCs, step by step](docs/lab-rollout.svg)

[At a glance](#at-a-glance) · [Quick start](#quick-start) · [Architecture](#architecture) ·
[The rollout](#the-rollout) · [Failure modes](#failure-modes-tested-on-the-lab) · [Testing](#testing) ·
[Redfish in practice](#redfish-in-practice) · [Supply chain](#firmware-supply-chain) ·
[Inventory](#inventory-netbox) · [Observability](#observability) · [Grafana](#grafana-dashboards) ·
[Production](#production) · [To do](#what-needs-to-be-done) · [Layout](#repository-layout)

## At a glance

| Concern | How it is handled |
|---|---|
| Blast radius | a canary per hardware model, then waves that grow (prod: 5 %, 25 %, 100 %), spread across racks (`max_per_rack`) |
| Gates | the canary and the first wave halt on any failure; later waves halt over `failure_rate` (prod: 2 %); a soak re-runs the post-check before the next wave |
| Post-update health | the target version runs, System and Manager health is no worse than before, no new failed Task or Job |
| Rollback | reinstall the version that ran before, reset, verify again; a host that can't be recovered stays drained and locked as `needs_attention` |
| Image integrity | signatures checked against a pinned key in a sandbox with no network, at ingest; sha256 checked against the catalog by the agent before every push |
| Change control | `plan.json` freezes the inputs (file sha256s, NetBox entries, each BMC's identity); later stages refuse anything that changed. Baseline and catalog are YAML reviewed in git |
| Concurrency | one PostgreSQL lock per host, taken all-or-nothing per stage; locks never expire, a person releases them |
| Audit | a run record per stage, written before and after every step; an append-only events table; `report.json` with who planned, approved and ran it |
| Observability | an exporter per site → Prometheus; rollout events, BMC `LogService` entries and, on the lab, the BMCs' pushed events (`EventService` → Vector) → Loki; Grafana per site |
| Tests | 33 unit tests on recorded Redfish data, 48 mutation cases, 6 fault-injection scenarios on the lab |

## Quick start

Needs Linux x86-64, Docker with Compose, Python 3.10+, `curl` and `openssl`, and Grafana's admin account in
`control/observability/grafana/.grafana.env` (git-ignored):

```sh
GRAFANA_ADMIN_USERNAME=...
GRAFANA_ADMIN_PASSWORD=...
```

```sh
make setup        # venv
make test         # unit tests, no BMC needed
make lab-up       # control plane, lab site, OpenBMC's two newest builds, 10 BMCs and their event subscriptions (~10 min)
make collect      # read every BMC; then make health, make firmware, ...
make dry-run      # planning of the rollout - read only
make update       # update
make report       # the latest pipeline's report
make fault SCENARIO=hybrid   # different scenarios for pipelines
make lab-reset    # stop the services and clear the volumes
```

Grafana: http://127.0.0.1:3000. NetBox: http://127.0.0.1:8000. `make help` lists every target. `SITE=prod` reads
the real fleet; `make update` and `make fault` refuse it. To roll the lab back, swap the commented line in
`control/catalog/lab/baseline.yaml` and run `make update` again.

## Architecture

![The lab, drawn on the production system's layout](docs/lab-solution-architecture.svg)

Three parts, one Compose file each, laid out as distributed systems. In the lab they share one host, every port on
127.0.0.1 but the event receiver's (:8088), which the BMCs reach over their own network, behind its credentials.

| Part | Runs | Connects to |
|---|---|---|
| **Control plane**, one per fleet: [control/](control/docker-compose.yaml) | image ingest and approved store (SeaweedFS), NetBox, the firmware catalog (YAML file in git), the state database (PostgreSQL), Prometheus, Pushgateway, Loki, Grafana | - |
| **Site**, one per site: [site/](site/docker-compose.yaml) | the exporter, a Prometheus agent (writes to Prometheus control instance), the HTTPS image cache (nginx), the event receiver (Vector, the lab); `rollout.py run` as the site agent | control plane; the BMCs post their events to it |
| **Lab hardware**: [lab/](lab/docker-compose.yaml) | ten emulated BMCs | |

**A lab BMC** is a container running OpenBMC's `qemu-system-arm` with the `gb200nvl-bmc` machine: a full emulation of
the AST2600 BMC (ARM cores, RAM, 64 MiB SPI flash, NIC) booting OpenBMC's GB200 NVL build. Only the BMC is emulated:
there is no Grace CPU or Blackwell GPU, so the lab updates the BMC's own firmware. bmcweb
serves Redfish on `127.1.0.N:443` (`bmc1` to `bmc10`, in 3 racks), login `root` / `0penBmc`, OpenBMC's public
default. Each BMC has its own flash volume, so an update survives a restart. Compose boots them in batches of four
to avoid CPU starvation with healthchecks per BMC.

![The lab stack: the firmware's way in, the scripts and the ten QEMU BMCs they update](docs/lab-stack.svg)

**Where the data lives:**

| Store | Written by | What | Kept |
|---|---|---|---|
| Prometheus | the exporter, through the site's agent | every reading and threshold, health, firmware versions, links, BMCs answering | 30 days |
| Pushgateway → Prometheus | `rollout.py run`, after every event | hosts per wave and state, versions, step durations, post-check results, gates | until restart; 30 days in Prometheus |
| Loki | `rollout.py`; the exporter; Vector | every rollout event, with its detail; each BMC's `LogService` entries; the lab BMCs' pushed events | 30 days |
| PostgreSQL | `rollout.py run --yes` | `host_locks`; `events`, append-only; `host_state`, each host's latest state | until deleted |
| `<site>/runs/` | `rollout.py` | `plan.json`, the run records (`.jsonl`), `report.json` and `report.html` | local files |

The Pushgateway is a lab shortcut: `rollout.py run` is a short-lived command. In production the rollout runs as
long-lived services that serve their own `/metrics`.

**What each path takes from the BMC.** Only two processes talk to a BMC: the exporter, read-only and always, and
`rollout.py`, which reads and writes during a rollout. The BMC itself pushes to Vector. Prometheus, its agent, the
Pushgateway and Loki never touch a BMC.

```
BMC ──GET whole tree · 10 s lab, 30 s prod──▶ exporter ──/metrics──▶ agent ──remote write──▶ Prometheus
BMC ──GET LogService entries · 5 min────────▶ exporter ──────────────push──────────────────▶ Loki {job="bmc-log"}
BMC ◀─GET + POST · during a rollout────────── rollout.py ──PUT──▶ Pushgateway ◀─scrape 5 s── Prometheus
                                              rollout.py ───────────────push───────────────▶ Loki {job="rollout"}
BMC ──POST each event · as it happens───────▶ Vector ────────────────push──────────────────▶ Loki {job="bmc-event"}
```

| Path | Over Redfish | Stored |
|---|---|---|
| exporter → agent → Prometheus | GET the whole tree but logs, sessions, accounts, certificates, registries: `Status` (health), `Sensor`, `Thermal`, `Power` (readings, thresholds), `SoftwareInventory` and `UpdateService` (version, `Updateable`, push/pull, A/B bank), `EthernetInterface` and `Port`, `ComponentIntegrity`, `ManagerDiagnosticData` | numbers over time: `redfish_*` |
| exporter → Loki | GET each `LogService` of the systems, managers and chassis, and `JobService.Log`: its new `Entries`, its `DateTime` (the BMC's clock) and `MaxNumberOfRecords` | a line per entry; each log's size and the clock's skew go to Prometheus |
| `rollout.py` → Pushgateway → Prometheus, and → Loki | a live read of 10-30 GETs per host (systems, managers and their health and identity, `UpdateService`, the firmware inventory, Tasks, Jobs, `ComponentIntegrity`, the reset ActionInfo); then the writes: the image push, `Manager.Reset` or `ComputerSystem.Reset`, and the update's Task polled to its end | the rollout's decisions, not BMC data: `rollout_*` metrics (hosts per state, step durations, gates), and in Loki each step with its checks, the Task's messages and a rollback's reason |
| BMC → Vector → Loki (the lab) | nothing read: the BMC posts its `Event` payloads (`MessageId`, `Message`, `Severity`, `EventTimestamp`, `OriginOfCondition`) to the subscription `rollout.py subscribe` made | a line per event, labelled with the BMC and its severity |

The BMCs know nothing of canaries, waves or gates, so the rollout's stream stays even where every BMC pushes; the log
reads stay too, for what a lost delivery misses. Detail: [BMC events, pushed](#observability).

## The rollout

### Pipeline

[pipeline.sh](pipeline.sh) runs **four stages**, for one firmware component per pipeline (the BMC by default):

1. **plan**: read every BMC live, pre-flight it, place the hosts that pass in waves by the site's policy file `policy.yaml`. 
`plan.json` records the sha256 of the baseline, catalog and policy, each host's NetBox entry, each BMC's identity (model, service UUID, serial), who planned it (`ROLLOUT_ACTOR`) and the git commit each catalog file was approved in.
2. **canary**: one host per hardware model, hosts tagged `canary`.
3. **waves**: growing waves, a gate after each, a soak ( wait time ) between them.
4. **report**: terminal, HTML and `report.json`, from the plan and the run records.

### Per-host steps

The hosts of a wave run in parallel (`max_parallel`). Each step is written to `<site>/runs/<pipeline>-<stage>.jsonl`
before and after it runs, so a run stopped halfway shows where each host was.

| # | Step | What it does |
|----|----|----|
| 1 | Pre-flight | Fresh read & the BMC must be the one the plan approved (model, service UUID, serial) |
| 2 | Drain | only when the reset restarts the host (BIOS, system firmware); In the LAB there is no Drain -> BMC reset. |
| 3 | Update | multipart push to `MultipartHttpPushUri`, or `SimpleUpdate` from a URL, with `OperationApplyTime: OnReset`; the Task followed to its end, every `TaskState`, `PercentComplete` and message recorded. **In LAB no Task migrate to Job.** |
| 4 | Reset | `Manager.Reset` or `ComputerSystem.Reset`, graceful first, then wait for the BMC to answer |
| 5 | Post-check | the target version runs, health no worse than before (or a resource gone from the telemetry), no new failed or hung Task or Job. In LAB the BMCs don't expose ComponentIntegrity Redfish resource. On a failure: reinstall the version before, reset, check again |
| 6 | Undrain | back to the scheduler; a host that can't be recovered stays drained as `needs_attention` - remote hands - and keeps its lock in the state DB. |

The post-check records how it knows the running version: `self-reported`, the firmware's own `SoftwareInventory`, or
`SPDM offered (<uri>), not verified` when the BMC exposes a `ComponentIntegrity` for the component. Neither the lab
nor any production model offers one, so every update reads `self-reported`.

### Pre-flight checks

Each host gets **go**, **skip** (nothing to do, or busy: a later wave) or **block** (a person has to look), with
every reason. It blocks:

- a downgrade, or a version order it can't tell, unless `--allow-downgrade`; dotted integers compare as numbers, so
  2.9 → 2.10 is an upgrade
- a component Redfish can't update (`Updateable: false`, `WriteProtected`), a target below `LowestSupportedVersion`
- a missing image, a sha256 mismatch, an image over `MaxImageSizeBytes`, an update method the BMC doesn't allow
- no rollback path: no image of the running version, unless `--accept-no-rollback`. An A/B bank doesn't count, as
  switching banks isn't built
- a reset that restarts the host with no drain configured
- a disabled `UpdateService`, health not `OK` on a System or Manager, a failed Task or Job

### Policy

Each site has a `policy.yaml`, reviewed like code: it decides how many hosts a bad image reaches before something stops
it. An option overrides one value for one plan; the plan and the report record what was used.

| Setting | [lab](lab/policy.yaml) | [prod](prod/policy.yaml) | What it does |
|---|---|---|---|
| `canary_per_model` | 1 | 1 | hosts of each model that go first: firmware faults are model-specific |
| `waves` | 33, 100 | 5, 25, 100 | cumulative % of the other hosts done after each wave |
| `max_per_rack` | 2 | 1 | hosts of one rack in a wave. At 1, 1,000 hosts in 100 racks take 11 waves |
| `strict_waves` | 1 | 1 | the first waves after the canary that halt on any failure |
| `failure_rate` | 10 % | 2 % | later waves halt above this share of the hosts tried; exactly the limit passes |
| `max_parallel` | 10 | 50 | updates at once in a wave |
| `soak` | 60 s | 30 min | after each wave but the last: wait, post-check its hosts again; a failure is `needs_attention` and halts |
| `drain`, `undrain` | none | none | scheduler commands, run only for a reset that restarts the host |

The lab's ten BMCs give: canary bmc1, wave 1 of 3 (one per rack), wave 2 of 6. The failure rate counts the hosts
tried in that stage: skipped and blocked hosts are left out, and the canary is its own stage. So a passed gate doesn't
prove every planned host updated; the report's `left_out` lists the rest.

### Locks, cancel, audit

- A stage takes the lock of every host it will touch in one transaction, or none: a host another pipeline holds stops
  the stage. Without the state database, `run --yes` doesn't start.
- At the stage's end the locks go back, except a `needs_attention` host's. A crash or Ctrl-C keeps all of them: a host
  may be mid-flash. Locks never expire; `make state` shows them, `make unlock HOST=...` releases one.
- `make cancel` stops cleanly: the wave running finishes, no wave starts after it, the locks go back, and the report says CANCELLED and who asked.
- Every event goes into `events` too. The rollout's account may insert rows, never change or delete one.
- Nothing resumes a stopped run: it is planned again, and pre-flight skips the hosts already on the target.

Two pipelines after one host, on the lab: B, started 3 s after A with the same plan, reached its canary while A held
the lock. B took no lock and wrote no event.

```
━━━ stage canary · 15:01:34 UTC ━━━
locked: 127.1.0.1 by pipeline-20261014T150128Z. One pipeline at a time touches a host: wait for it to end, or make unlock HOST=... once a person checked the host
━━━ stage canary: FAILED, exit 1 after 2s ━━━
```

### Reports

One report per pipeline that changed something or stopped, in two forms drawn from the same data:

- **`report.json`** (schema `rollout-report/1`), for services: `result` (`passed`, `halted`, `cancelled`, `blocked`,
  `incomplete`, `nothing to do`), where it stopped and why, who planned, approved and ran it, the fleet's versions
  before and after, `action_needed`, and per host its state, versions, identity and steps with their evidence: every
  pre-flight check, the update's method, URI, image sha256 and Task with every `MessageId`, the reset, the post-check,
  the rollback. UTC ISO 8601 times, fixed state words.
- In the terminal and as `report.html`: the verdict first, then what needs an operator, the versions, and
  every host by wave with its gate. `make report HOST=127.1.0.3` shows the host selected.

An update of all ten lab BMCs: the canary, then waves of 3 and 6, every gate passed.

![The report of an update of all ten BMCs](docs/runs/update.svg)

## Failure modes tested on the lab

`make fault SCENARIO=<name>` runs the real pipeline with a fault injected where a real one would show
([lab/scenario.sh](lab/scenario.sh)); the script checks how each host ended. The single-fault scenarios hit every host,
so the canary fails and the waves never run:

| Scenario | The bad update | What the rollout does | Canary ends |
|---|---|---|---|
| `silent-fail` | the Task says `Completed`, but the firmware doesn't change | post-check **failed** → reinstall the version before → **success** | `rolled_back` |
| `unhealthy` | the new firmware boots, then fails the post-check | reflash the version before, check again | `rolled_back` |
| `rejected` | the BMC refuses the image (cut to 8 MiB): Task `Exception` | no reset, old firmware still runs: nothing to roll back | `failed` |
| `bad-checksum` | the file doesn't match the catalog's sha256 | pre-flight blocks every host; the plan fails | `blocked` |
| `no-return` | the BMC isn't back within the reset timeout | the rollback's reset times out too: nothing verified, locked for a person | `needs_attention` |
| `hybrid` | canary fine; in wave 1, one good host, one `unhealthy`, one `rejected` | wave 1 is strict: halt; wave 2 never starts | `updated` |

A canary can't catch a fault that hits only some hosts; the gate after each wave stops it spreading. After `hybrid`
the fleet is on two builds, and the report says which host runs which.

![The report of the hybrid scenario](docs/runs/hybrid.svg)

<details>
<summary>The other scenarios' reports, and one host's evidence</summary>

![One host's steps: every pre-flight check, the push and its task, the reset, the failed post-check, the rollback](docs/runs/unhealthy-host.svg)
![The report of silent-fail](docs/runs/silent-fail.svg)
![The report of unhealthy](docs/runs/unhealthy.svg)
![The report of rejected](docs/runs/rejected.svg)
![The report of bad-checksum](docs/runs/bad-checksum.svg)
![The report of no-return](docs/runs/no-return.svg)
![The terminal of make update](docs/runs/update-live.svg)

</details>

## Testing

| Layer | Command | What it proves |
|---|---|---|
| Unit | `make test`: 31 tests, about a second, no BMC | the decisions that stop a bad image, on a real lab BMC's Redfish data ([testdata](tests/testdata/openbmc-gb200.json)) with one fact changed per case. `make update` runs it first |
| Mutation | `make mutations`: 48 cases | each safety rule broken the way a careless edit would; the test that guards it must fail. 48 of 48 do |
| Fault injection | `make fault SCENARIO=…` | the real pipeline on the lab's BMCs, past the network boundary the unit tests stop at |

| What [tests/test_rollout.py](tests/test_rollout.py) covers | Cases |
|---|---|
| Pre-flight | go, skip and block as listed above, each on its own |
| Post-check | target running; health no worse; a manager gone fails; a new failed task fails; `Completed` with the old firmware fails |
| One host | failure before the reset: nothing to roll back; after it: the version before is reinstalled, not the target; failed or impossible rollback, failed drain or undrain: `needs_attention`; a different BMC than planned is blocked |
| Gates | strict waves halt on any failure; later waves over `failure_rate`, exactly the limit passes; a soak failure halts |
| Change control | a changed or missing input file stops the run; `run --yes` refuses a host not tagged `writable` before any lock |
| Locks, events | all or none; a `needs_attention` host keeps its lock; the rollout's account can't change or delete an event |
| Inventory | NetBox paging; `writable` only from a tag; a device without an HTTPS address or a properly named account is skipped |
| Ingest | packages unsigned, re-signed, tampered with, for another machine or holding a link are refused, and with them the batch |
| BMC events | a writable host subscribed once, to `/events/<host>` with the receiver's credentials; kept on the next run, deleted with `--remove`; an untagged host never touched |
| Image cache, attestation, logs | a bad cached copy is deleted; SPDM only when it covers the part; log reads survive Loki down, clock skew, a cleared log, a restart |

The mutations are 48 careless edits: a downgrade let through, versions compared as text, a sha256 mismatch ignored,
the rollback installing the target, the gate halting at exactly `failure_rate`, a held lock let through, `writable`
taken from a config context, a secret sent as a BMC login, a log line lost while Loki is down… The first run found a
real gap: the strict-wave case had enough failures to halt anyway, so it passed with wave 1 not strict. The gate and
the post-check were written test-first. There are no CI runners: `make update` runs `make test` and stops on a failure.

## Redfish in practice

Checked on real Redfish services with the latest DMTF schemas.

**The lab's OpenBMC (bmcweb):**

- `UpdateService` (v1_11_1) advertises no `@Redfish.OperationApplyTimeSupport`. Tried each: `OnReset` gets 202 and a
  Task; `OnTargetReset`, `AtMaintenanceWindowStart`, `InMaintenanceWindowOnReset` and `OnStartUpdateRequest` get 400
  `PropertyValueNotInList`.
- A 64 MiB push takes about a minute; its Task goes Running → Completed in 5 s with the BMC still up. The image is
  applied at the Manager's reset (back in about 215 s); the host isn't restarted.
- `OnReset`, not `Immediate`: with `Immediate` the BMC flashes and reboots inside the Task, and bmcweb drops its tasks
  when it reboots, so the Task's end can't be observed.
- `SoftwareInventory` v1_1_0 has no `Staged` (v1_12_0) or `Armed` (v1_15_0) and there is no `Activate`: a pending
  image is invisible, and the reset is the only way to apply it.
- A Task, not a Job: `GET /redfish/v1/JobService` answers 404. No `SimpleUpdate`, so the lab runs push only.
- An aborted Task stays in `TaskService` until the BMC restarts, so pre-flight blocks that BMC until then.
- Events: `EventService` v1_5_0 takes a subscription with a plain-HTTP `Destination`, posts its JSON indented over many
  lines (a receiver must take the whole body as one payload), and leaves the subscription's `Context` out of a test
  event, so the destination URL names the BMC instead. Its server-sent event stream answers 404. For an image it
  refused, it pushed `TaskEvent.1.0.TaskCompletedWarning` while the Task itself ended in `Exception`: an event is a
  hint, and the rollout still polls the Task it started to decide.

**The production fleet:**

- One session per BMC, reused while valid: a login per read adds a log entry each time (an iLO logs every login). A
  401 on a fresh login isn't retried until a restart, as BMCs lock the account after a few failures.
- A reading's unit comes from its `MetricDefinition`, else its resource's DMTF schema (`ReadingCelsius` is `Cel`), else
  `ReadingUnits`, else the Sensor schema's unit for its `ReadingType` (Gigabyte's v1.0 Sensors report none). A
  threshold of 0 means none is set (iLO 4, AMI's lower thresholds).
- A full iDRAC 8 crawl takes about two minutes, longer than prod's 30 s interval: each BMC has its own loop, with a
  budget of three intervals.
- `$filter` can't ask for new log entries only: a Supermicro answers `Created gt …` with 400, bmcweb returns the
  whole log.
- `$expand` and `$select` gain nothing here, though the service root's `ProtocolFeaturesSupported` advertises both on
  every BMC but iLO 4, so the crawl stays one GET per resource. `$select` only trims a body, it never saves a GET.
  `$expand=.($levels=1)` against the plain crawl, one host per model: the iDRAC 8 stopped answering, the Gigabyte JBOD
  took 141 s instead of 78 s, the Gigabyte LQC5 returned 458 of its 734 resources, the ASUS took as long, and the older
  Supermicro answered the root with 500. Only the newer Supermicro gained, 11 s instead of 14.5 s. AMI also leaves
  `@odata.type` out of inline members. On the lab's bmcweb, 46 GETs became 17 in the same ~2 s, as it serves one
  request at a time.
- What a model keeps outside the standard around updates is in its Oem extensions, which `make oem [MODEL=...]`
  lists per model and BMC firmware, update-related first: iDRAC 8's update jobs (Oem `DellJob`, its job queue), AMI's
  second BMC image (`DualImageConfigurations`: a Gigabyte's image 2 runs 13.06.10, invisible to the standard A/B
  check), HPE's backup BIOS ROM, Supermicro's `SmcUpdateService.Install`, and the resources a license or role refuses
  (403). It is discovery, to qualify a model: pre-flight and the post-check judge standard Jobs and Tasks only, as an
  Oem path can change with the firmware the rollout installs.

| BMC | Log entry order | Paging | Clock |
|---|---|---|---|
| Supermicro | oldest first | one page | on time |
| AMI (Gigabyte, ASUS) | oldest first | 50 entries, `$skip` | Gigabyte 71 s ahead; ASUS 5 h ahead: UTC labelled `-05:00` |
| iDRAC | newest first | 50 entries, `$skip` | 5 h 52 min ahead: UTC labelled `-05:00`, and 52 min off |
| iLO 4 | links only, 30 listed | no `Members@odata.nextLink`, so only 30 are read; ~4 s per entry | no `DateTime` to correct by |

## Firmware supply chain

1. **Download** ([download-fw-package.sh](control/ingest/download-fw-package.sh)): OpenBMC's two newest GB200 NVL
   builds over HTTPS into `control/ingest/staging/`. Jenkins keeps only its last three builds, so none can be pinned.
   It never opens a package.
2. **Verify** ([verify-fw-package.sh](control/ingest/verify-fw-package.sh)), the only code that opens an outside
   package, in the `sandbox` service: no network, read-only root, `nobody`, no capabilities, 256 MiB tmpfs, inputs
   read-only. A package passes only if:
   - it holds only an OpenBMC package's files, each a regular file, checked before anything is unpacked: no path,
     link or device is ever written;
   - `MANIFEST`, `publickey` and `image-bmc` each have an RSA-SHA256 signature that verifies against the key pinned in
     the repository ([openbmc-dev.pub](control/ingest/openbmc-dev.pub)), never the key the package carries, which an
     attacker would replace together with the image;
   - its verified `MANIFEST` says it is a BMC image for `gb200nvl-obmc`.

   One package that fails stops them all: nothing is listed.
3. **Promote** ([promote-store-fw-package.sh](control/ingest/promote-store-fw-package.sh)): checks each listed
   package's sha256 again (one changed since the sandbox is refused), uploads it into the store, writes the catalog
   (`images.yaml`: path and sha256) and approves the newer build in `baseline.yaml`.
4. **Distribute**: each site's nginx cache (HTTPS, 2 GiB, 30 days) fetches from the store on a miss. Pre-flight
   downloads the image into the agent's spool and checks its sha256 against the catalog before a byte reaches a BMC;
   a copy that doesn't match is deleted.

**In LAB**: the pinned key is OpenBMC's development key, whose private half is public. In the lab the checks
show the mechanism and protect nothing; production pins each vendor's key and downloads on a quarantine host whose
only egress is the vendor. The lab's SeaweedFS store doesn't enforce immutable, digest-addressed objects, and an
admin with Docker access can bypass the ingest.

| | Push: `MultipartHttpPushUri` | Pull: `SimpleUpdate` |
|---|---|---|
| Who downloads | the site agent, from the site's cache | the BMC, from the cache URL the agent hands it |
| sha256 against the catalog | by the agent, before every push | only at promotion: nothing checks what the BMC downloads |
| What keeps it the approved image | the agent's check | a digest-addressed URL and an immutable store |
| For | a BMC that can't pull, like the lab's | a BMC that allows the URL's protocol and target |

## Inventory: NetBox

NetBox, in the control plane, is the only inventory for both sites. The poller, the exporter and
the rollout read their site's devices over its REST API. Baseline, catalog and policy stay YAML in git, where a merged pull request is the approval.

| The tools read | From NetBox |
|---|---|
| the BMC's address | the device's out-of-band IP, on its management-only interface `bmc` |
| vendor, model, project, rack | manufacturer, device type, tenant, rack |
| canary, writable | tags; only a tag makes a host writable |
| how its BMC is reached | the merged site and platform config contexts: TLS verify, retries, deadline, and the names of the env vars holding its account |

A device is skipped when it has no out-of-band IP, isn't reached over HTTPS, or its account isn't named
`X_USERNAME`/`X_PASSWORD` with both set: an edit in NetBox can't make a tool send another of its secrets (`STATE_DB`,
`NETBOX_TOKEN`…) as a BMC login, or send one in clear. Each service has its own NetBox account and read-only token that
may only view devices; a write gets 403. Who may edit devices in NetBox still decides where a BMC account is sent.

<details>
<summary>The lab in NetBox (7 screenshots)</summary>

![NetBox: the ten lab servers, each with its rack, model and out-of-band IP](docs/netbox/devices.png)
![NetBox: bmc1, its out-of-band IP 127.1.0.1 and its device type GB200 NVL](docs/netbox/device.png)
![NetBox: bmc1's management-only interface bmc with 127.1.0.1/32](docs/netbox/interfaces.png)
![NetBox: the ten BMC addresses, 127.1.0.1 to 127.1.0.10](docs/netbox/ip-addresses.png)
![NetBox: the config context site lab: verify false, retries 1, deadline 300, LAB_USERNAME and LAB_PASSWORD](docs/netbox/config-context.png)
![NetBox: the permissions, each service's account may only view DCIM devices](docs/netbox/permission.png)
![NetBox: the API tokens, one read-only token per service](docs/netbox/tokens.png)

</details>

## Observability

**The exporter**, `poller.py exporter`, one per site (lab every 10 s on `:9101`, prod every 30 s on `:9102`), crawls
each BMC on its own loop and serves the latest read; a scrape never triggers a crawl. The site's Prometheus agent
scrapes it and remote-writes to the control plane, so nothing connects into a site.

| Metric | What |
|---|---|
| `redfish_up`, `redfish_failed_requests` | the BMC answered; GETs that failed in the read |
| `redfish_host_info` | NetBox's vendor, project and rack; the BMC's Manufacturer and Model |
| `redfish_health` | every object with a `Status`, fans and PSUs included |
| `redfish_reading`, `_threshold`, `_crossed` | every sensor with its unit and `PhysicalContext`, its thresholds, how far past them (0 to 3) |
| `redfish_firmware_info` | each component's version, `Updateable`, push and pull support, and whether it reports an A/B bank (`ab_bank`) |
| `redfish_network_link`, `redfish_network_speed_mbps` | every interface and port |
| `redfish_component_integrity` | each `ComponentIntegrity` (SPDM, TPM) and the BMC's verdict |
| `redfish_log_entries`, `redfish_log_max_entries`, `redfish_bmc_clock_skew_seconds` | how full each log is; the BMC clock's offset |

**The BMCs' logs in Loki.** Every 5 minutes, over the same session, the exporter pages the entries of every
`LogService` under the Systems, Managers and Chassis, and the JobService's `Log` (not OpenBMC's `Journal`), and
pushes the new ones to Loki as `{job="bmc-log", site, host, log, severity}`, with the standard properties only. No
BMC here reports a JobService log yet; the Gigabyte's JobService has none, the lab's has no JobService. TaskService
has no log in the standard: tasks are read by pre-flight and the post-check, and recorded per pipeline.

| When | The exporter |
|---|---|
| Loki down, 5xx or 429 | counts nothing as sent; retries next pass (`redfish_log_pending`) |
| Loki answers 400 | doesn't resend; counts it (`redfish_log_refused`) |
| It restarts | goes on from a per-host state file, like Promtail's positions; without one, from what Loki has of the last 29 days. Loki alone can't be the state: a restart minutes after a first read stored 521 of 539 lines again on a Supermicro |
| The BMC clock is off by 5 min or more | moves entries onto our clock, by whole minutes |
| An entry has no usable time | stamps the read time and keeps `Created` in the line |
| A log wrapped or was cleared | writes a Warning `Gap:` line |
| A log is large (iDRAC Lifecycle: 4190 entries) | reads newest-first logs until a page with nothing new; stops a pass after 120 s |

**Rollout events** are written to the local run record first, then pushed to Loki labelled only `job` and `site`; the
pipeline, host and step are JSON fields, so the stream count stays flat. Delivery is best effort: after a failed push,
the sender stops forwarding for that run and the local record stays complete.

**BMC events, pushed (the lab).** `make subscribe`, which `make lab-up` runs, gives each BMC tagged `writable` one
`EventService` subscription: its `Destination` the lab site's receiver at `/events/<the BMC>`, the receiver's
credentials in its `HttpHeaders`. Run again, it keeps what is there; `ARGS=--remove` deletes ours. The receiver is
Vector ([site/vector.yaml](site/vector.yaml)): its `http_server` source checks the credentials (anything else gets
401), a `remap` makes each Event of a payload one line, and its `loki` sink writes it as
`{job="bmc-event", site, host, severity}`, shown on the Host dashboard under the BMC's logs. A BMC whose post fails
tries again (`DeliveryRetryAttempts`: 3), and the 5-minute log reads stay, for what a lost delivery misses. Tried on
the lab: ten subscriptions, a test event (`SubmitTestEvent`) from bmc3 in Loki within seconds, a delivery Vector
refused retried by the BMC, and in a real update every BMC's own `TaskEvent.1.0.TaskStarted` and `TaskCompletedOK`,
which the Host dashboard's timeline shows between the rollout's `update: start` and `update: Completed`. Production
waits: a subscription is a write. `vector test` checks the transform without a BMC.

**Grafana** is admin only, no anonymous access. A Lab and a Prod folder hold the same four dashboards, each fixed to
its site, plus Update details in Lab: only the lab's are in git, and `make control-up` generates the prod ones
([prod_dashboards.py](control/observability/grafana/prod_dashboards.py)). A local **Reports** folder holds incident
reports on real hosts, git-ignored. Prometheus's admin API (127.0.0.1 only) deletes a pipeline's history:
`delete_series?match[]={pipeline="<pipeline>"}`, then `clean_tombstones`.

| Dashboard | Shows |
|---|---|
| Fleet manager | BMCs answering, worst health, a row per host; Redfish up, down or updating over time (an update's reset isn't an outage); firmware by model and version, where two versions on one model is drift |
| Host | one BMC: firmware, how it can be updated and its A/B bank, every component's health, its logs and pushed events, a timeline of them beside the rollout's steps on it, network links, every reading per `PhysicalContext` against its thresholds, its update history from the state database |
| Pipeline | one rollout: verdict, counts, each host's pre-flight and wave, the gate after each wave, each host's numbered steps, the decision log, timelines, errors, the locks now |
| Update details (lab) | the Redfish facts above, and per pipeline each host's evidence: pre-flight checks, push, Task, reset, rollback |
| Architecture | the diagrams |

## Grafana dashboards

Each screenshot is a real run: the production fleet, read-only, and the lab's ten BMCs during an update where every
gate passed and during each [bad update](#failure-modes-tested-on-the-lab). The lab emulates only the BMC, so its
thermal, power and threshold panels are empty; the production screenshots show them full.

### Fleet manager

Every BMC at a glance: how many answer, the worst health, a row per host with its equipment and rack, Redfish
answering over time, firmware per model and version. A BMC that stops answering while a pipeline updates it shows as
**updating** (blue), not down, so a firmware reset isn't counted as an outage.

The production fleet:

![Prod · Fleet manager](docs/grafana/prod/fleet.png)

![Prod · Fleet manager: firmware, ComponentIntegrity by host, and the readings nearest their thresholds](docs/grafana/prod/fleets-details-2.png)

![Prod · Fleet manager: what is not OK, and crawl times](docs/grafana/prod/fleet-details-1.png)

The lab: every BMC, then firmware per version and per host, and the BMCs' own memory and storage

![Lab · Fleet manager](docs/grafana/lab/fleet.png)

![Lab · Fleet manager: firmware per version and per host](docs/grafana/lab/fleet-firmware.png)

![Lab · Fleet manager: the BMCs' memory, storage and crawl times](docs/grafana/lab/fleet-1.png)

During `no-return`: bmc1 isn't back after its reset, `down` at the top of Hosts and red in Redfish answering

![Lab · Fleet manager: bmc1 not answering during no-return](docs/grafana/lab/fleet-down.png)

After `hybrid`: every BMC answers and is OK, but two BMC versions run on one model, the drift the report shows

![Lab · Fleet manager: after hybrid, two Manager (BMC) versions on one model](docs/grafana/lab/fleet-drift.png)

### Host

One BMC in full: its firmware, how Redfish can update it and whether it reports an A/B bank (a second image, inactive,
staged or armed, beside the running one), every component's health worst first, its logs from Loki, the events it
pushed, and a timeline that puts both beside each step a rollout took on it (counts stay on the logs alone: a pushed
event usually shows again as a log entry), its network links, every reading per `PhysicalContext` against its caution and critical thresholds, and every firmware
update of the host from the state database.

A production Supermicro SYS-221H-TNR

![Prod · Host: a Supermicro SYS-221H-TNR, its firmware and the health of every component](docs/grafana/prod/supermicro.png)

Its BMC logs over 7 days: Critical and Warning entries, the Maintenance Event Log 99.6 % full (it overwrites its
oldest entries next), entries over time by severity; then its ComponentIntegrity

![Prod · Host: the BMC's logs from Loki, and its ComponentIntegrity](docs/grafana/prod/supermicro-logs.png)

![Prod · Host: thermal and power](docs/grafana/prod/supermicro-details.png)

![Prod · Host: the BMC and its Redfish service](docs/grafana/prod/supermicro-details-2.png)

A lab BMC: its firmware and health; what it pushed through its EventService, and the timeline that puts it beside
each step the rollout took on it; the BMC's CPU; and the BMC and its Redfish service

![Lab · Host: one emulated BMC, its firmware and the health of every component](docs/grafana/lab/host.png)

![Lab · Host: the events the BMC pushed, and the rollout's steps beside them by time](docs/grafana/lab/host-events.png)

![Lab · Host: the BMC's CPU](docs/grafana/lab/host-sensors-2.png)

![Lab · Host: the BMC's memory, storage, crawl time, failed GETs and Redfish answering](docs/grafana/lab/host-details-1.png)

### Pipeline

One rollout as an operations page: the run, the verdict, the counts, the plan with each host's pre-flight and wave,
the gate after each wave, every host's numbered steps (1 pre-flight to 6 undrain, then the soak), the decision log,
timelines, every error, and the host locks from the state database. **Gate markers** at the top draws each gate's
decision on the timelines.

**Every gate passed**: the canary, then waves of 3 and 6

![Lab · Pipeline: every gate passed](docs/grafana/lab/pipeline-pass.png)

![Lab · Pipeline: every host's numbered steps, the checks, and the decision log](docs/grafana/lab/pipeline-pass-hosts.png)

![Lab · Pipeline: one host's every event](docs/grafana/lab/pipeline-pass-host-events.png)

![Lab · Pipeline: every host's step over time, and the timing of each step](docs/grafana/lab/pipeline-pass-timing.png)

**`unhealthy`**: the canary failed its post-check and was rolled back; the gate halted before wave 1, and the other
nine BMCs were never touched

![Lab · Pipeline: the canary rolled back, the pipeline halted](docs/grafana/lab/pipeline-rollback.png)

![Lab · Pipeline: the canary's steps, its rollback, and the decision log](docs/grafana/lab/pipeline-rollback-hosts.png)

![Lab · Pipeline: the errors, and the failed check](docs/grafana/lab/pipeline-rollback-errors.png)

**`silent-fail`**: the Task ended `Completed` and the BMC came back on the old version; the running-version check
caught it, and the rollback reinstalled it

![Lab · Pipeline: silent-fail, the canary's steps, its rollback, and the decision log](docs/grafana/lab/pipeline-silent-fail-hosts.png)

![Lab · Pipeline: silent-fail, the errors and the failed running-version check](docs/grafana/lab/pipeline-silent-fail-errors.png)

**`rejected`**: the BMC refused the image, its Task ended in `Exception` before any reset: the old firmware still
runs, nothing to roll back

![Lab · Pipeline: rejected, the canary's Task ended in Exception](docs/grafana/lab/pipeline-rejected-hosts.png)

![Lab · Pipeline: rejected, the errors](docs/grafana/lab/pipeline-rejected-errors.png)

**`no-return`**: the BMC wasn't back after its reset, nor after the rollback's: nothing verified, `needs_attention`,
locked for a person

![Lab · Pipeline: no-return, the canary needs a person](docs/grafana/lab/pipeline-no-return.png)

![Lab · Pipeline: no-return, the canary's steps and the decision log](docs/grafana/lab/pipeline-no-return-hosts.png)

![Lab · Pipeline: no-return, the canary's steps over time and Redfish answering](docs/grafana/lab/pipeline-no-return-timing.png)

![Lab · Pipeline: no-return, the errors](docs/grafana/lab/pipeline-no-return-errors.png)

**`hybrid`**: in wave 1, bmc2 updated, bmc3 was rolled back, bmc4 refused the image; wave 1 is strict, so wave 2
never started

![Lab · Pipeline: hybrid, halted after wave 1](docs/grafana/lab/pipeline-hybrid.png)

![Lab · Pipeline: hybrid, every host's steps and the decision log](docs/grafana/lab/pipeline-hybrid-hosts.png)

![Lab · Pipeline: hybrid, every host's step over time, and the timing of each step](docs/grafana/lab/pipeline-hybrid-timing.png)

The same run on the Host dashboard, each BMC's pushed events beside the rollout's steps. bmc3, rolled back: two
update tasks, the target and then the reinstall of the version before, around the failed post-check

![Lab · Host: bmc3's events and the rollout's steps, its update, the failed post-check and the rollback](docs/grafana/lab/host-events-rollback.png)

bmc4, whose BMC refused the image: it pushed `TaskCompletedWarning`, "completed with warnings", while its Task ended
in `Exception`. The rollout judges the Task, not the event: `update: Exception`, `done: failed`

![Lab · Host: bmc4's events and the rollout's steps, the refused image](docs/grafana/lab/host-events-rejected.png)

**`bad-checksum`**: pre-flight blocked every host on the image's sha256, so the plan halted and no stage ran;
Update details shows the one check that failed

![Lab · Pipeline: bad-checksum, halted at the plan, every host blocked](docs/grafana/lab/pipeline-bad-checksum.png)

![Lab · Update details: bad-checksum, image sha256 failed on every host](docs/grafana/lab/pipeline-bad-checksum-checks.png)

### Update details

The lab only: how a BMC firmware update runs here, with the [Redfish facts](#redfish-in-practice) checked against the
DMTF schemas and on a lab BMC (`OnReset`, the Manager's reset alone, a Task and no Job, nothing staged to cancel) and
the rollback per resource; then one pipeline's evidence, stage by stage.

The facts, and the rollback per resource the rollout updates

![Lab · Update details: four facts checked against Redfish, and the rollback per resource](docs/grafana/lab/details.png)

The plan's pre-flight of every host, each check against every host ✓ or ✗ with why, then step 1's pre-flight again on a
fresh read, with the BMC's identity

![Lab · Update details: the plan's pre-flight of every host, and step 1's pre-flight again](docs/grafana/lab/details-plan.png)

Steps 2 to 4 and the rollback: the drain, the push with its `OnReset`, the Task, the Manager's reset, each host's
rollback

![Lab · Update details: the evidence of every host](docs/grafana/lab/details-evidence.png)

## Production

### Today: read-only

`make collect`, the views, `make plan` and `make dry-run` with `SITE=prod`, and the prod site's exporter have run on
the real fleet: reads only, plus a session login and logout. The Supermicro has no baseline row yet, so the plan skips
it. A host is added in NetBox: a device of site `prod` (manufacturer, tenant, rack), a management-only interface `bmc`
with the BMC's address as the out-of-band IP, and a platform (`DELL BMC`, `HPE BMC`…) whose config context names the
account's env vars, which `prod/.env` (git-ignored) sets:

```json
{"username_env": "DELL_REDFISH_USERNAME", "password_env": "DELL_REDFISH_PASSWORD"}
```

### Before enabling writes

- **Accounts**: the poller stays on a ReadOnly role; the rollout gets its own account with firmware-update and reset
  privileges, verified per BMC. The `writable` tag is an application gate, not proof of a Redfish role.
- **Scope**: tag hosts `writable` only for the change window, each with its rack, and a few spare hosts per model
  `canary`.
- **Activation**: validate the supported apply time, reset and recovery per model, and read `make oem MODEL=...` for
  what the model keeps in Oem (a job queue, a second image). Only OpenBMC's path has been exercised end to end.
- **Scheduler**: write and test `drain` and `undrain` (`{node}`); the drain returns once the node is empty.
- **Images**: the vendor packages in `prod/images.yaml` with their sha256, and the packages of the versions running
  now, so every host has a way back; served from the site's HTTPS cache (`IMAGE_CACHE`).
- **Run** from an admin host, never the Makefile: `SITE=prod YES=1 ./pipeline.sh "Manager (BMC)"` with `prod/.env`
  exported, `NETBOX_TOKEN` the rollout's token, one `STATE_DB` for every admin host, and `ROLLOUT_ACTOR` naming who
  authorized it.

The rollout order: rings (the lab, then internal and spare nodes, then production region by region), one component
per pipeline (the BMC first, as it carries the other updates, then BIOS, then the rest), and production's policy:
canary, 5 %, 25 %, 100 %, the first wave strict, later ones halting over 2 %, 30 minutes of soak.

### Target architecture at fleet scale

![A production firmware-update system](docs/prod-solution-architecture.svg)

The BMC stays the only truth about what runs: a host is read again over Redfish before anything acts on it.

| Part | Target | This repository today |
|---|---|---|
| Fetch | a quarantine host, egress to the vendor only | `download-fw-package.sh` on the control host |
| Verify | the sandbox, each vendor's key pinned per model | the same sandbox, OpenBMC's development key |
| Qualify | lab hosts of every model before approval | none |
| Approved store | S3 or Artifactory, digest-addressed, immutable | SeaweedFS, build-number names |
| Site cache | nginx over HTTPS per site | the same, in the lab site |
| Catalog, baseline | YAML in git, every change checked in CI | YAML in git; the plan freezes what it used |
| Inventory | NetBox or Nautobot, SSO, backed up | NetBox, a read-only account per service |
| API, approval | a service, with an approval between the canary and wave 1 | the CLI: `make`, `YES=1` |
| Workflow engine | Temporal or Argo Workflows: resumes after a crash | `pipeline.sh` and `rollout.py run`; no resume |
| Rollout state | PostgreSQL for every site, the gate as a query, partitioned retention | PostgreSQL: locks, append-only events, no retention |
| Site agent | long-lived, one per site, its own `/metrics` | `rollout.py run` on an admin host, pushing to a Pushgateway |
| Scheduler | Slurm or Kubernetes, through a validated integration | hooks, none configured |
| Provisioning | Ironic or Metal3 owns each host's power, boot and reinstall. The rollout either takes the host from it (Ironic maintenance mode, Metal3's detached annotation) before taking its lock and gives it back after, or hands it the update (`HostFirmwareComponents`, an Ironic service step). Never two actors on one BMC: a power sync during the update's reset, or a reinstall mid-flash | none: the rollout is the only actor on a BMC |
| In-band updates | an in-band runner per site, its own task queue: `ansible-runner` (or Salt) on one host, SSH, the vendor's tool | none: Redfish only |
| Secrets | Vault | git-ignored env files |
| Alerts | Alertmanager pages on a halt or `needs_attention` | none |
| BMC events | `EventService` subscriptions to a receiver per site (Vector), forwarding to Loki | the logs, polled every 5 minutes; the lab's BMCs push theirs to Vector |

### The rollout in a workflow engine

In production a rollout runs as a workflow in a durable engine, Temporal (or Argo Workflows on Kubernetes), not as
`pipeline.sh` on an admin host. The engine takes the orchestration; the per-host steps and their Redfish code don't
change, they become the engine's activities, run by each site's agent.

| Today: `pipeline.sh`, `rollout.py run` | In the engine |
|---|---|
| a process per stage; a crash keeps the locks, and a person plans the run again | one workflow per rollout: its history is replayed after a crash, and it goes on from the last step that finished |
| the soak a `sleep` | a durable timer, which a restart doesn't reset |
| `YES=1` before the run starts | a signal: a person approves wave 1 after the canary, through the API |
| each host's steps in a thread on an admin host | activities on the site's task queue, run by that site's agent, the only one that reaches its BMCs; the agent polls the engine, so the site still only connects out |
| `make cancel` writes a file | a cancellation: the wave under way finishes, no wave starts after it |
| metrics pushed to a Pushgateway | the agent's and the engine's own `/metrics`, scraped |

What stays: the plan frozen as the workflow's input, pre-flight on a fresh read before each host, the gates, a lock
per host in the state database, the run record and `report.json`. Why not make `pipeline.sh` resume instead: after a
crash mid-flash the BMC may or may not have taken the image. The engine knows which steps finished, and the push and
the reset are never retried blindly: the activity reads the BMC again first (is the target running, is a task still
open) and acts on what it finds.

### Out-of-band and in-band updates, one pipeline

The stages and their rules are the same for every component: the plan and pre-flight, the canary, waves, gates,
locks, soak and evidence; each host goes pre-flight, drain, update, activate, post-check, undrain or roll back. Only
the update step on the host differs, chosen per component, and the plan records which path each host's component
takes, as it records push or pull today.

Each site is two networks, and each path has its own worker on its own: out-of-band, the BMC management network, holds
the site agent, the image cache, the BMCs and the event receiver; in-band, the hosts' network, holds the in-band
runner, the hosts and the scheduler. The drain and the undrain go through the scheduler's API (`scontrol` or
`slurmrestd`, or the Kubernetes API), so they are the in-band runner's steps for both paths. The image cache serves
both networks (or each has its own, filled from the same approved store), and both workers check an image's sha256
against the catalog before it reaches a device.

Only the workflow engine spans the two: both workers connect out to it, and it orders their steps. A BIOS update is
an in-band drain, an out-of-band push, reset and post-check, then an in-band check from the host (`nvidia-smi`,
`ibstat`) and undrain, each step on the worker that reaches it. One path joins the networks inside each server: the
Redfish Host Interface (DSP0270), the host OS's own link to its BMC over an internal USB NIC. Keep it disabled, or
limited to an account that may only read.

#### Out-of-band: Redfish, over the BMC

- **When**: the component is in the BMC's firmware inventory, `Updateable`, with an update method the BMC allows.
- **How**: Redfish `UpdateService`, a push or `SimpleUpdate`, `Targets` naming the component; the Task followed to
  its end.
- **Activate**: the BMC's or the host's reset.
- **Verify**: the running version in `SoftwareInventory`, health, no new failed Task.
- **Pre-flight blocks**: `Updateable` false, a method the BMC doesn't allow.

More components take this path on newer platforms: the BMC updates the devices behind it (GPUs, NICs, retimers) with
PLDM for Firmware Update (DSP0267) over MCTP (DSP0236), so they show in its firmware inventory and take a standard
`UpdateService` call. How the BMC reaches the device is its own business.

#### In-band: Ansible, over SSH to the host

- **When**: Redfish doesn't reach the component on this platform.
- **How**: the site's in-band runner, a worker of its own on its own task queue, runs `ansible-runner` (or Salt) on
  one host: SSH, the image from the site's cache with its sha256 checked, then the vendor's tool, `mstflint` or
  `mlxup` for ConnectX and InfiniBand, `nvme-cli` for drives.
  The workflow engine hands it the step, as it hands the site agent its Redfish steps: the agent reaches only the
  BMCs' management network, the runner only the hosts over SSH, so neither worker reaches both.
- **Activate**: `mlxfwreset`, or a reboot.
- **Verify**: the same tool's query (`mstflint query`), `ibstat`.
- **Pre-flight blocks**: an OS that doesn't answer, the tool or the driver missing.

The vendor's tool isn't rewritten, only wrapped: one host per step, the version read first, then flash, activate,
read again, and JSON evidence back, which the workflow judges like any other step. In-band stays for what a platform
doesn't expose: one more kind of step in the same pipeline, never a second rollout system with weaker rules.

### What needs to be done

**Four standard Redfish features.** This version runs the first on the lab only, and can't build or test the other
three: the lab's emulated OpenBMC either doesn't offer them or has no hardware behind them, and production is
read-only while each one writes to the BMC. They need a lab host of each real model, with writes allowed.

| Feature | How, in standard Redfish | Why not today |
|---|---|---|
| **BMC events, pushed** (`EventService` → Vector → Loki) | POST an `EventDestination` to each BMC's `EventService/Subscriptions`: `Destination` the site receiver's HTTPS URL, `EventFormatType: Event`, filtered by `RegistryPrefixes`. The receiver, Vector's `http_server` source (or DMTF's [Redfish Event Listener](https://github.com/DMTF/Redfish-Event-Listener)), keeps `MessageId`, `Severity`, `EventTimestamp`, `OriginOfCondition` and the BMC, and its `loki` sink writes them. Alerts in seconds instead of the 5-minute log poll, which stays: a BMC drops an event after `DeliveryRetryAttempts`, its log keeps it. Subscriptions are checked and re-created on each pass, as a BMC reset to defaults loses them | Built and tried on the lab ([BMC events, pushed](#observability), `make subscribe`). Production: a subscription is a write, and the receiver needs HTTPS on the management network with a certificate the BMCs trust |
| **A/B bank rollback** | On a dual-bank BMC the version before stays in the inactive bank: switch to it (PATCH the Manager's `Links.ActiveSoftwareImage`, or an `Activate` action), reset, post-check. Faster than a reinstall, and no image to fetch. Pre-flight would count a bank as a way back only when it holds the version before and the switch is offered | The lab's BMC reports one image (`SoftwareInventory` v1_1_0: no `Staged`, no `Activate`). Production's AMI BMCs keep their second image only in Oem (`DualImageConfigurations`, [`make oem`](#redfish-in-practice)), and a switch is a write |
| **SPDM attestation** (`ComponentIntegrity`, `SPDMGetSignedMeasurements`) | In the post-check, for an enabled SPDM `ComponentIntegrity` covering the updated part, POST `SPDMGetSignedMeasurements` with a fresh nonce; verify the certificate chain to the vendor's root and the signature with libspdm, and each measurement against reference values recorded at qualification. A mismatch fails the post-check like a wrong version, and the version reads `SPDM verified` | No `ComponentIntegrity` anywhere: QEMU emulates only the BMC, with no device behind it, and all seven production models report none (`make capabilities SITE=prod`). The POST is outside read-only production |
| **Jobs: a Task handing over to a Job** (`JobService`) | A BMC that schedules an update as a Job links it from the Task (`Links.CreatedResources`) and runs it at a maintenance window or on reset (`AtMaintenanceWindowStart`, `InMaintenanceWindowOnReset`, `OnStartUpdateRequest` with `UpdateService.StartUpdate`). Follow the Task to its Job, read `JobState`, `PercentComplete` and `Messages`, and choose the apply time from the BMC's `@Redfish.OperationApplyTimeSupport` | The lab answers `JobService` 404 and refuses every apply time but `OnReset` (400). In production only the Gigabyte has a `JobService` (v1_0_0), read-only here, and iDRAC 8 keeps its jobs in Oem (`DellJob`) |

**Then, for a fleet:**

- **Firmware beyond the BMC**: BIOS, NICs and InfiniBand HCAs, GPUs, PSUs, CPLDs. The plan already reads every
  firmware inventory row and its update targets, and the order is the BMC first, then BIOS; only the BMC has been
  updated end to end. A BIOS update restarts the host, so it needs the scheduler's drain. Firmware Redfish doesn't
  reach (ConnectX through `mstflint` or `mlxup` in the OS) needs the in-band step on the same pipeline
  ([out-of-band and in-band](#out-of-band-and-in-band-updates-one-pipeline)).
- **The scheduler**: a validated drain and undrain through Slurm or Kubernetes, for every update that restarts the
  host.
- **Resume**: a stopped run keeps its locks; a person checks and unlocks the hosts, and it is planned again. Durable
  orchestration belongs to [the workflow engine](#the-rollout-in-a-workflow-engine).
- **Alerts**: Alertmanager paging on a halt, a host left `needs_attention`, a BMC down outside an update, a log about
  to wrap.
- **A rollout API**, with an approval between the canary and wave 1, which a person, Salt or Ansible can call; the CLI
  only today.
- **Limits beyond racks** (per PDU, fabric pod or spare capacity), and **rings**: rolling out region by region.
- **Pull integrity**: what a BMC pulls is never seen by the agent, so its sha256 isn't checked against the catalog; it
  needs a digest-addressed, immutable store.

## Repository layout

```
Makefile        every command: make help
pipeline.sh     plan → canary → waves → report
rollout.py      plan, run, report
poller.py       collect, the views (health, firmware, inventory, telemetry, capabilities, diff, oem) and the exporter
redfish.py      the Redfish connection both tools share: session, token, GET, POST, DELETE
constants.py    what to crawl, table columns, rollout constants
helpers.py      shared helpers
tests/          test_rollout.py (make test), mutations.py (make mutations), testdata/: a lab BMC's Redfish data
control/        the control plane: ingest/ (download, sandbox verify, promote, the pinned key), store/, netbox/seed.py,
                state/schema.sql, observability/ (Prometheus, Loki, Grafana), catalog/<site>/ (baseline, images)
site/           a site: the exporter's Dockerfile, the Prometheus agent, cache/ (nginx), vector.yaml (the event receiver)
lab/            the lab: the BMCs' Dockerfile and Compose, scenario.sh, policy.yaml, prometheus-agent.yml
prod/           the real fleet's site: policy.yaml, prometheus-agent.yml; .env stays local
docs/           diagrams; runs/: reports of real lab runs; grafana/, netbox/: screenshots
```

Secrets stay local and git-ignored: `make` generates the control plane's `.netbox.env`, `.tokens.env` and `.state.env`
with random values; you write `.grafana.env` and `prod/.env`.
