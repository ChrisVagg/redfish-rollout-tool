# Redfish firmware rollout

Firmware updates for server BMCs over the standard [Redfish](https://www.dmtf.org/standards/redfish) API. Production
has only been read (monitoring, plans, dry runs); every update so far ran on the lab. `make update` refuses
production, and `rollout.py` itself updates only hosts marked `writable: true`, and only with `--yes`
([Production](docs/prod.md)). Four parts, each run with `make` ([Quick start](#quick-start-the-lab)):

| Part | What it does |
|---|---|
| **Poller** ([poller.py](poller.py)) | Polls the resources each BMC's Redfish service exposes: health, firmware, inventory and sensor readings, as views of a collect or, run without stopping, as metrics. Reads only. |
| **Rollout** ([rollout.py](rollout.py), [pipeline.sh](pipeline.sh)) | The firmware updates: pre-flight, a canary, then waves, each after a gate, with a rollback when a host fails its post-check. |
| **Lab** ([lab/](lab/)) | Ten emulated OpenBMC BMCs, each QEMU in its own Docker service: the only BMCs the rollout updates. Beside them, the image store (SeaweedFS), which takes only packages whose signatures verify, and the site's image cache in front of it (nginx, HTTPS), which the rollout gets every firmware image from. |
| **Observability** ([observability/](observability/docker-compose.yaml)) | Docker services: `exporter-lab` and `exporter-prod` (the poller, serving metrics), `pushgateway` (where the rollout pushes its metrics), `loki` (where it sends every event of its run record), `prometheus` and `grafana`. |

Two more pages go deeper:

| Page | What it covers |
|---|---|
| [The lab](docs/lab.md) | The ten emulated BMCs and how they are built; a firmware image's way from OpenBMC's builds to a BMC's flash; how an update runs on these BMCs, checked against the Redfish standard; the bad-update scenarios; every Grafana panel over the lab |
| [Production](docs/prod.md) | The real fleet, read-only so far; how a production rollout is built and run; the fleet in Grafana; the update as Redfish resources; the system the rollout grows into at fleet scale |

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
pipeline is planned and run again, and pre-flight skips the hosts already on the target ([Not built yet](#not-built-yet)). Each pipeline keeps its plan and reports in `<site>/runs/pipeline-<time>/`.

## What a run looks like

Every image here is a real run on the lab: its report, as `rollout.py report --svg` draws it from the pipeline's plan
and run records.

**An update of all ten BMCs**: the canary, then waves of 3 and 6, every gate passed.

![The report of an update of all ten BMCs](docs/runs/update.svg)

<details><summary>The terminal while it ran: every stage, and every step of every BMC as it happened</summary>

![The terminal of make update](docs/runs/update-live.svg)

</details>

**A rollout that goes wrong** (`hybrid`): wave 1 has a good host, one whose new firmware fails its post-check and is
rolled back, and one whose BMC refuses the image. Wave 1 is strict, so the pipeline stops there and wave 2 never
starts. The other bad updates, one fault each, are under [Bad updates](docs/lab.md#bad-updates).

![The report of the hybrid scenario](docs/runs/hybrid.svg)

## Quick start: the lab

Needs Linux x86-64, Docker with Compose, Python 3.10+, `curl`, `openssl`, and an admin account for Grafana in
`observability/grafana/.grafana.env`, git-ignored like `prod/.env`:

```sh
# observability/grafana/.grafana.env, read by docker compose
GRAFANA_ADMIN_USERNAME=...
GRAFANA_ADMIN_PASSWORD=...
```

```sh
make setup        # venv/ and requirements.txt
make test         # the rollout's decisions on recorded Redfish data, no BMC needed (Test-driven development)
make lab-up       # the lab: starts the image store, ingests OpenBMC's two newest builds into it (download,
                  # signatures checked, upload), downloads QEMU (~230 MB in all), boots 10 BMCs (~10 min)
make monitor-up   # observability: exporters, Pushgateway, Loki, Prometheus, Grafana on http://127.0.0.1:3000
make collect      # the poller: crawl every BMC into lab/snapshots/
make health       # a view of that collect: health per host, then everything not OK
make dry-run      # the rollout, nothing written: plan, canary, waves
make update       # the rollout: a canary of 1 BMC, then waves of 3 and 6
make report       # the verdict, what needs a person, every host by wave (HOST=… for its steps)
make lab-reset    # stop the BMCs and wipe their flash: they boot the older build again; the store keeps its images
```

Every target runs on the lab; add `SITE=prod` for the real fleet, which is only ever read: `make update` and
`make fault` refuse it. `make help` lists every target and the variables it takes. To roll the lab back, swap the
commented line in `lab/baseline.yaml` and run `make update` again. Grafana at <http://127.0.0.1:3000>, admin login
only, has a **Lab** and a **Prod** folder, each with the same three dashboards fixed to its site: **Fleet manager**,
every BMC, **Host**, one BMC in full, and **Pipeline**, which follows a rollout live ([Observability](#observability)).

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

<details><summary>One host's steps with their evidence: the canary of <code>unhealthy</code></summary>

![One host's steps: every pre-flight check, the push and its task, the reset, the post-check that failed, the rollback](docs/runs/unhealthy-host.svg)

</details>

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

Every panel, as screenshots: [the lab](docs/lab.md#the-lab-in-grafana) and [the production fleet](docs/prod.md#the-production-fleet-in-grafana).

**Two stores, two ways in.** Prometheus pulls: every 10 s it fetches `/metrics` from each site's exporter, and every
5 s from the Pushgateway, and the exporter itself polls each BMC over Redfish, so what changes between two reads goes
unseen. Loki is pushed to: `rollout.py` sends each event the moment it records it. Prometheus keeps numbers over time,
labelled (hosts per state, seconds per step, every sensor reading), for graphs, counts and thresholds; Loki keeps the
events themselves with their detail (each step's outcome, the Task's messages, the checks), for what happened to one
host and why. In production the BMCs' own events reach Loki the same way, pushed through each BMC's Redfish
`EventService`, as they happen; the lab's bmcweb build advertises its server-sent event stream but answers it with a
404, so the lab doesn't collect them yet.

Grafana is admin only: no anonymous access, the one account from `observability/grafana/.grafana.env`, which
[grafana.ini](observability/grafana/grafana.ini) reads. Grafana creates it on its first start only; after that, change
its password in the UI. The admin can change and save anything in the UI, and it stays in Grafana's volume across
`make monitor-down` and up, except the six dashboards: every Grafana start loads them again from
[observability/grafana/dashboards/](observability/grafana/dashboards/). To keep a change to one, export it over its
lab file (Export → Export as JSON), or save it as a copy. Only the lab dashboards are kept in git: `make monitor-up`
makes the prod ones from them ([prod_dashboards.py](observability/grafana/prod_dashboards.py): the same panels fixed
to site prod, and a read-only note on the pipeline), so a change is made once and both folders get it.

What the exporter does differently from a one-off `collect`, because it never stops:

- **One Redfish session per BMC, kept across reads while the BMC keeps it.** A login per read adds an entry to each
  BMC's own log (iDRAC's Lifecycle Log, iLO's security log): 288 a day at 5 minutes. Dell and Supermicro keep a session
  30 minutes (`SessionService.SessionTimeout`); AMI (ASUS, Gigabyte) 30 seconds, so there the exporter logs out right
  after each read and frees the slot. A session the BMC ends anyway (a reset) gets a new login and the read again. On
  `docker stop` it logs out of every session: a BMC allows only a handful.
- **A refused login stops that host.** A 401 on a fresh login is not retried until a restart, since BMCs lock the
  account after a few failures; `redfish_login_refused` says so.
- **Prod every 5 minutes.** A full read of an iDRAC 8 takes about 2 minutes, one GET at a time, so a read every 5 minutes
  leaves the BMC free most of the time. Each BMC has its own loop: a slow one never delays the others, and a scrape
  always gets the latest finished read.
- **Units and thresholds from the standards.** A reading's unit comes from the BMC's `MetricDefinition`, else the
  DMTF schema of its resource (`ReadingCelsius` is `Cel`), else its `ReadingUnits`, else the unit DMTF's newest Sensor
  schema gives its `ReadingType` (a Gigabyte's v1.0 Sensors report none). Thresholds take the Sensor schema's names
  (`UpperThresholdNonCritical` in Thermal is `UpperCaution`); a 0 is none set (iLO 4, and AMI's lower thresholds).
- **No task queue.** Prometheus schedules the scrapes and the exporter its reads. Celery would add a broker, workers
  and a beat scheduler for one periodic read; durable, resumable jobs, like a rollout, belong to the workflow engine
  of [a production system](docs/prod.md#a-production-system---scaling-for-a-large-fleet).

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
real faults ([Bad updates](docs/lab.md#bad-updates)): the push, the task, the reset, the wait. A change starts the same way: a
test that fails, then the code that makes it pass.

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
| `drain`, `undrain` | none | a Slurm example, commented | The scheduler's commands, run only for a reset that restarts the host; pre-flight blocks such an update without a drain. The drain must return only once the node is empty: Slurm's `DRAIN` only stops new jobs, so the example waits until `sinfo` says `drained`. A drain past 24 h fails the host. |

With the lab's 10 BMCs in 3 racks: canary 2441 (fw image), wave 1 of 3 (one per rack), wave 2 of 6 (two per rack).

## Not built yet
- **Other update flows**: switching to the other A/B bank as a rollback (pre-flight doesn't count a bank as a way
  back), an `Activate` action, a staged image, an apply scheduled as a Job. The rollout uses an upload applied at
  reset, its own reset, and a reinstall of the version before as the way back.
- **Durable orchestration**: a lock per host, so two rollouts never touch one; picking up a stopped run where it
  was, after a crash; resolving a request whose answer was lost (an update the BMC may or may not have taken). The run
  records say what happened, but nothing resumes from them: a stopped pipeline is planned and run again, and
  pre-flight skips the hosts already on the target. In production this is the workflow engine and the rollout state
  database ([a production system](docs/prod.md#a-production-system---scaling-for-a-large-fleet)).
- **Limits beyond racks**: per PDU, fabric pod or remaining capacity.
- **Pull integrity**: a pulled image is never seen by the agent, so its sha256 isn't checked against the catalog;
  it relies on the URL pointing at the store's copy, which never changes, and the BMC's own signature check.
- Rolling out ring by ring or region by region.
- **A rollout API**: an operator starts and approves a rollout only from the CLI (`make`, `YES=1`).

## Layout

```
Makefile            every command: make help
pipeline.sh         plan → canary → waves → report
poller.py           collect, the views (health, firmware, inventory, telemetry, capabilities, diff) and the exporter
redfish.py          the Redfish connection both tools share: session, token, GET, POST, DELETE
rollout.py          plan, run, report
test_rollout.py     the rollout's decisions on recorded Redfish data: make test
mutations.py        breaks rollout.py one rule at a time; each break must fail a test: make mutations
fixtures/           the lab BMC as a collect saved it, which the tests read
constants.py        what to crawl, table columns, rollout constants
helpers.py          shared helpers
schemas/            DMTF Redfish JSON Schemas for sensor units, downloaded on first use (git-ignored)
lab/                the emulated BMCs: Dockerfile, docker-compose.yaml, scenario.sh, inventory.yaml, rollout.yaml;
                    download-fw-images.sh and promote-fw-images.sh, the ingest, with openbmc-dev.pub, the gate's
                    pinned key; store/, the image store's read-only access; cache/, the image cache's nginx config
prod/               the real fleet: baseline.yaml, rollout.yaml; inventory.yaml and .env stay local
observability/      Compose for the exporters, Pushgateway, Loki, Prometheus and Grafana; loki/loki.yaml; a Lab and a
                    Prod dashboard folder;
                    grafana/grafana.ini; grafana/.grafana.env, Grafana's admin account, stays local;
                    grafana/prod_dashboards.py makes the prod dashboards from the lab ones
docs/               lab.md and prod.md; the diagrams: the rollout, the lab, production today, a production system,
                    the lab on its layout, one update as resources; grafana/, a screenshot of every panel
docs/runs/          reports of real lab runs: the update and every bad-update scenario
```
