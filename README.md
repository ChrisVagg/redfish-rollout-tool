# Redfish firmware rollout

Firmware updates for server BMCs over the standard [Redfish](https://www.dmtf.org/standards/redfish) API. Production
is read-only; writes happen only in the lab. Four parts, each run with `make` ([Quick start](#quick-start-the-lab)):

| Part | What it does |
|---|---|
| **Poller** ([poller.py](poller.py)) | Polls the resources each BMC's Redfish service exposes: health, firmware, inventory and sensor readings, as views of a collect or, run without stopping, as metrics. Reads only. |
| **Rollout** ([rollout.py](rollout.py), [pipeline.sh](pipeline.sh)) | The firmware updates: pre-flight, a canary, then waves, each after a gate, with a rollback when a host fails its post-check. |
| **Lab** ([lab/](lab/)) | Ten emulated OpenBMC BMCs, each QEMU in its own Docker service: the only BMCs the rollout updates. Beside them, the site's image cache (nginx, HTTPS), which the rollout gets every firmware image from. |
| **Observability** ([observability/](observability/docker-compose.yaml)) | Docker services: `exporter-lab` and `exporter-prod` (the poller, serving metrics), `pushgateway` (where the rollout pushes its metrics), `prometheus` and `grafana`. |

## How the rollout works

![The rollout](docs/rollout.svg)

Each site is a folder, `lab/` or `prod/`, picked with `SITE=lab` or `SITE=prod`. Three files describe its fleet:
`inventory.yaml` (the BMCs, how to reach them, their rack, whether they are `writable`), `baseline.yaml` (the approved
version per model and component) and `images.yaml` (the image file and sha256 per version).

[pipeline.sh](pipeline.sh) runs the canary first, then wave by wave, with a gate after each:

1. **plan**: reads every BMC live and pre-flights it, then puts the hosts that pass in waves as the site's rollout
   policy says ([Rollout policy](#rollout-policy)): a canary of each hardware model, then waves that grow, spread
   across racks. It saves `plan.json`, and the later stages run exactly that plan.
2. **canary**: updates the canary hosts. Gate: any failure stops the pipeline, so wave 1 never starts.
3. **waves**: wave 1 starts only after the canary passed and soaked. After each wave, the gate: the first waves are
   strict like the canary (any failure stops the pipeline); later ones stop when more than `halt_at` of the hosts
   tried failed. A wave that passes soaks too, then its hosts are checked again before the next wave starts.
4. **report**: one report of the whole pipeline, from the plan and the run records (see [Reports](#reports)). It
   runs even after a stop; a stopped pipeline exits 1.

Every host, in the canary and in each wave, goes through six steps; the hosts of one wave run side by side:

1. **pre-flight** again, on a fresh read: the host may have changed since the plan.
2. **drain**, only when the reset restarts the host (BIOS, system firmware); a BMC reset leaves the host running.
3. **update**: multipart push of the image, or `SimpleUpdate` from a URL, applied on reset. The BMC runs it as a
   `Task` of its `TaskService`; every change of `TaskState`, `PercentComplete` and `Messages` is recorded, and the
   report shows the task, its messages and its progress under the step.
4. **reset**: `Manager.Reset` or `ComputerSystem.Reset`, graceful first, then wait until it answers again.
5. **post-check**: the target version runs, health is OK and no new job failed or stale.
6. **rollback**. When the post-check failed, roll back first: reinstall the version from before, reset, check
   again. A host that can't be rolled back stays drained and needs attention from a person in site(`needs_attention`).
7. **undrain**. If the post-check pass undrain the nodes - hosts.

Pre-flight gives each host **go**, **skip** (nothing to do, or busy: try a later wave) or **block** (a person has to
look), with every reason. It blocks a downgrade or a version order it can't tell (unless `--allow-downgrade`), a
component Redfish can't update (`Updateable` false, `WriteProtected`), a target below `LowestSupportedVersion`, a
missing image, a sha256 mismatch, an image over `MaxImageSizeBytes`, no rollback path (no A/B bank and no image of the
running version, unless `--accept-no-rollback`), a disabled update service, health not OK, and failed jobs.

Every step goes into `<site>/runs/<run id>.jsonl`, its intent before it and its result after, so a run stopped halfway
still shows where each host was. Each pipeline keeps its plan and reports in `<site>/runs/pipeline-<time>/`.

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
starts. The other bad updates, one fault each, are under [Bad updates](#bad-updates).

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
make lab-up       # the lab: downloads QEMU and two OpenBMC builds (~230 MB), boots 10 BMCs (~7 min), starts
                  # the image cache
make monitor-up   # observability: exporters, Pushgateway, Prometheus, Grafana on http://127.0.0.1:3000
make collect      # the poller: crawl every BMC into lab/snapshots/
make health       # a view of that collect: health per host, then everything not OK
make dry-run      # the rollout, nothing written: plan, canary, waves
make update       # the rollout: a canary of 1 BMC, then waves of 3 and 6
make report       # the verdict, what needs a person, every host by wave (HOST=… for its steps)
make lab-reset    # stop the BMCs and wipe their flash: they boot the older build again
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

## The lab stack

![The lab](docs/lab.svg)

Each lab BMC is four layers in one Docker container ([lab/](lab/)):

| Layer | What it is |
|---|---|
| Docker | [lab/Dockerfile](lab/Dockerfile): Debian slim with QEMU and the flash image. [Compose](lab/docker-compose.yaml) runs ten, `bmc1` to `bmc10` (about 2 GiB of RAM each), each with its own flash volume, so an update survives a restart. Healthy once `/redfish/v1` answers. |
| QEMU | OpenBMC's prebuilt `qemu-system-arm` with the `gb200nvl-bmc` machine: a full emulation of the BMC's own computer, the ASPEED AST2600 chip with its ARM cores, RAM, SPI flash and NIC. Only the BMC is emulated; there is no Grace CPU or Blackwell GPU behind it, so the lab updates the BMC's own firmware. |
| OpenBMC | The GB200 NVL build (`gb200nvl-obmc`) from OpenBMC's Jenkins: Linux, D-Bus and the phosphor services, booted from a 64 MiB flash image. |
| bmcweb | Redfish on the BMC's port 443. QEMU forwards it to the container, and Compose publishes it on `127.0.0.1:2441-2450` (SSH on `2221-2230`). Login `root` / `0penBmc`, OpenBMC's public default. |

`make lab-up` runs [lab/images.sh](lab/images.sh), which fetches the two newest GB200 NVL builds (Jenkins keeps only
the last three, so no build can be pinned). The lab boots the older one and `lab/baseline.yaml` approves the newer, so
the first rollout is a real update. Both update packages go into `lab/images.yaml` with their sha256.

Ten emulated BMCs booting at once starve each other of CPU, so they boot in two batches (`bmc6` to `bmc10` 90 s later).
Even so, a service now and then runs before `/dev/mtd/u-boot-env` exists: systemd ends degraded, and bmcweb reports
the Manager `Quiesced`, health `Critical`. `make lab-up` then restarts that BMC ([lab/heal.sh](lab/heal.sh)); pre-flight
would block it, as it would a real one.

Beside the BMCs, Compose runs the site's **image cache**: nginx serving `lab/images/` read-only over HTTPS on
`127.0.0.1:8443`, with a self-signed certificate `make lab-up` makes ([lab/cache/](lab/cache/nginx.conf)); nothing
outside `/images/` is served. The rollout gets every image from it, as a site agent does in production: pre-flight
downloads it into `lab/spool/` and checks it against the sha256 in `lab/images.yaml` (a copy that doesn't match is
deleted, to be fetched again), and the update pushes that copy. These BMCs can't pull an image themselves (their
UpdateService has no `SimpleUpdate`), so the lab runs the push half; pull, a URL handed to the BMC, is covered by
pre-flight's checks and the tests. The images are files on disk, the store's and the agent's copies; no database holds
them, only the catalog's path and sha256 for each.

**How an update runs on the lab's BMCs.** End to end, with no staging: these BMCs report `SoftwareInventory` v1_1_0,
without `Staged` or `Active` (v1_12_0), and their UpdateService has no `Activate` action, so an image can't be queued
for a later activation. The rollout pushes the image to `MultipartHttpPushUri` with `@Redfish.OperationApplyTime:
OnReset` (about a minute for 64 MiB), follows the Task the BMC creates (Running → Completed, 100 %, in 5 s, the BMC
still up), then activates the image with its own reset step, `Manager.Reset` `GracefulRestart` (the BMC is back in
about 4.5 minutes), and the post-check confirms the new version runs. `OnReset` rather than `Immediate`, which would
flash and reboot inside the task: the task's `Completed` is recorded before the BMC goes away, and the rollout chooses
the reset and times the return. bmcweb drops its tasks when it reboots, so with `Immediate` the task's end could not be
observed.

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
| `exporter-lab`, `exporter-prod` | `poller.py exporter`: crawls every BMC of its site, each on its own loop, and serves the latest read on `/metrics`: what the equipment is (`redfish_host_info`: the inventory's vendor, project and rack, the BMC's Manufacturer and Model), whether it answered (`redfish_up`) and how many of its GETs failed (`redfish_failed_requests`: what they would have read is missing from that read), every reading with its unit (`redfish_reading`), its thresholds (`redfish_reading_threshold`) and how far past them it is (`redfish_reading_crossed`: 0 within, 1 caution, 2 critical, 3 fatal, upper or lower), the health of every object with a `Status`, each fan and power supply too (`redfish_health`), each firmware component with its running version, whether Redfish can update it and how an image gets in, push or pull, the same checks as `rollout.py plan` (`redfish_firmware_info`), and the BMC's last reset, memory and free storage (`ManagerDiagnosticData`). Standard properties only, read as `collect` reads them. The lab every 10 s on `:9101`; prod every 5 min on `:9102`, read-only, with `prod/.env`'s accounts; without a `prod/inventory.yaml` (a fresh clone) it reads nothing and stays up. |
| Pushgateway `:9091` | Where `rollout.py run` pushes its report as metrics after every recorded event (`PUSHGATEWAY`): hosts by wave and state, versions, every step's duration, every check after the flash, each gate. Best effort: if it is down, the rollout logs it once and goes on. |
| Prometheus `:9090` | Scrapes both, labels each exporter's metrics with its `site`, keeps 15 days. |
| Grafana `:3000` | A **Lab** and a **Prod** folder, the same three dashboards in each, fixed to its site. **Fleet manager**, every BMC: BMCs answering, worst health, readings past critical, a row per host with its equipment (vendor, manufacturer, model, project, rack) and failed GETs that opens its own dashboard, Redfish up over time (a firmware update shows as the reset's gap), firmware by hardware model and version (two versions of one component on one model is drift) and by host with Updateable, Push and Pull, the readings nearest their upper critical, everything not OK, the BMCs' own memory and storage. **Host**, one BMC: what it is, its firmware inventory and what Redfish can update and how, the health of every component, worst first, then a row each for Chassis · Thermal, Chassis · Power, Systems and Managers (the BMC itself): a chart per kind of reading, in its unit, with min, max, mean and last in the legend and each reading's caution and critical thresholds as dashed lines (orange, red). **Pipeline**, per pipeline: hosts by state, each gate, versions before, target and after, the longest steps, failed checks, and its BMCs answering Redfish. |

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
  of [a production system](#a-production-system---scaling-for-a-large-fleet).

### The lab in Grafana

The Lab folder over six hours: the ten emulated BMCs, all answering and healthy, read every 10 seconds. The fleet
manager and the host dashboard show what they show for production, here with the BMC's own CPU, memory and storage,
which OpenBMC reports; the pipeline dashboard follows two real rollouts, one where every gate passed and one where the
canary failed its post-check (the `unhealthy` fault), was rolled back, and the gate halted the pipeline.

#### Fleet manager

![Lab · Fleet manager](docs/grafana/lab/fleet.png)

<details><summary>More of the fleet manager</summary>

![Lab · Fleet manager: the BMCs' memory, storage and crawl times](docs/grafana/lab/fleet-1.png)

</details>

#### Host

![Lab · Host: one emulated BMC](docs/grafana/lab/host.png)

<details><summary>More of the host</summary>

![Lab · Host: the BMC's CPU, memory, storage and Redfish service](docs/grafana/lab/host-details-1.png)

</details>

#### Pipeline

![Lab · Pipeline: every gate passed](docs/grafana/lab/pipeline-pass.png)

![Lab · Pipeline: the canary rolled back, the pipeline halted](docs/grafana/lab/pipeline-rollback.png)

### The production fleet in Grafana
 The fleet manager shows which BMCs answer, their health, equipment and firmware, and how each component can be updated. 
The host dashboard shows host in full: its firmware, the health of every component, and every reading
against its thresholds.

#### Fleet manager

![Prod · Fleet manager](docs/grafana/prod/fleet.png)

<details><summary>More of the fleet manager</summary>

![Prod · Fleet manager: firmware and the readings nearest their thresholds](docs/grafana/prod/fleets-details-2.png)

![Prod · Fleet manager: what is not OK, and crawl times](docs/grafana/prod/fleet-details-1.png)

</details>

#### Host

![Prod · Host: a Supermicro SYS-221H-TNR](docs/grafana/prod/supermicro.png)

<details><summary>More of the host</summary>

![Prod · Host: thermal and power](docs/grafana/prod/supermicro-details.png)

![Prod · Host: the BMC and its Redfish service](docs/grafana/prod/supermicro-details-2.png)

</details>

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
| `no-return` | The BMC isn't back within the reset timeout (45 s; a boot takes ~3 min) | the rollback reinstalls the version before, but its reset times out too: nothing verified, left for a person | `needs_attention` |

<details><summary>The report of <code>silent-fail</code></summary>

![The report of silent-fail](docs/runs/silent-fail.svg)

</details>
<details><summary>The report of <code>unhealthy</code></summary>

![The report of unhealthy](docs/runs/unhealthy.svg)

</details>
<details><summary>The report of <code>rejected</code></summary>

![The report of rejected](docs/runs/rejected.svg)

</details>
<details><summary>The report of <code>bad-checksum</code></summary>

![The report of bad-checksum](docs/runs/bad-checksum.svg)

</details>
<details><summary>The report of <code>no-return</code></summary>

![The report of no-return](docs/runs/no-return.svg)

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

## Test-driven development

`make test` runs [test_rollout.py](test_rollout.py) in under a second, with no BMC. It tests the decisions that keep a
bad image from spreading, on real Redfish data: the lab BMC as a collect saved it
([fixtures/openbmc-gb200.json](fixtures/openbmc-gb200.json)), with one fact changed per case.

| What decides | The cases |
|---|---|
| Pre-flight (`check`) | go; a skip when already on the baseline or nothing is approved for the model; a downgrade, or an order nobody can tell, blocked unless `--allow-downgrade` (dotted integers compare as numbers: 2.9 to 2.10 is up); a missing image, a sha256 that doesn't match, an image over `MaxImageSizeBytes`; no way back unless `--accept-no-rollback`; an unreachable BMC, `Updateable` false, a Critical manager or a failed task blocked, a running task skipped |
| Post-check (`after_checks`) | the target runs; health no worse than before; no new failed task. A task that says `Completed` while the old firmware still runs fails it |
| Rollback (`run_host`) | a failure before the reset: the old firmware still runs, nothing to roll back; after it: the version that ran before is reinstalled, not the target; a failed rollback, or none possible: `needs_attention` |
| The gate (`gate`) | any failure halts the canary and the strict waves; a later wave halts over `halt_at`, exactly `halt_at` passes; a host that failed its soak halts any wave; blocked and skipped hosts were never tried |
| The plan, update methods, metrics, the exporter | waves per model and rack; push or pull against what the BMC allows; metrics mid-run; one series per sensor |

Only the network is replaced, in `run_host`: the read and the update, which need a BMC. Everything that decides runs as
it does in a rollout.

A test is worth something only if it fails when the code is wrong. The gate and the post-check's decision were written
test first: the tests failed (there was no `gate` or `after_checks`), then the code moved out of `run` and
`post_check` until they passed. For the code that came before its tests, [mutations.py](mutations.py) (`make mutations`)
breaks one rule of `rollout.py` at a time the way a careless edit would: a downgrade let through,
versions compared as text, a sha256 mismatch ignored, the rollback installing the target, the gate halting at exactly
`halt_at`, and 13 more. For each break, the test that guards the rule must fail: 18 of 18 do. Its first run found a
gap: the strict-wave case had enough failures to halt anyway, so it passed with wave 1 not strict; the case now has 1
failure in 11 hosts.

The two run at different times. `make test` is the gate: it runs on every change, and `make update` runs it first, so
no real update starts on broken decision logic (the gate is local: no CI runners). `make mutations` takes a few seconds
and runs before a change to a safety rule: it finds each rule by its line, so a rule rewritten there must be rewritten
in its list too, and that would make a poor gate.

The unit tests stop at the network. Beyond it, `make fault SCENARIO=…` runs the real pipeline on the lab's BMCs with
real faults ([Bad updates](#bad-updates)): the push, the task, the reset, the wait. A change starts the same way: a
test that fails, then the code that makes it pass.

## Rollout policy

Each site has a `rollout.yaml` next to its inventory and baseline. `plan` and `run` read it, an option overrides one
value for one run (`--canary`, `--waves`, `--max-per-rack`, `--halt-at`), and the plan and the report record what was
used. It is reviewed like code: it decides how many hosts a bad image can reach before something stops it.

| Setting | [lab](lab/rollout.yaml) | [production](prod/rollout.yaml) | What it does |
|---|---|---|---|
| `canary_per_model` | 1 | 1 | Hosts of each hardware model that go first. A bad image is almost always model-specific, so one canary for the whole fleet proves nothing for the other models. Hosts marked `canary: true` in the inventory go first. |
| `waves` | 33, 100 | 5, 25, 100 | Cumulative % of the other hosts done after each wave: small while the evidence is thin, bigger once gates have passed. |
| `max_per_rack` | 2 | 1 | Hosts of one rack in the same wave, at most: a rack never loses more nodes than it can spare. The rest wait for a later wave, so it also caps a wave's size: 1,000 hosts in 100 racks at 1 per rack take 11 waves after the canary (50, then up to 100 each); at 2 per rack, 6. |
| `strict_waves` | 1 | 1 | The first waves after the canary that halt on any failure, as the canary does. |
| `halt_at` | 10% | 2% | Later waves stop when more than this share of the hosts tried failed. 10% of 1,000 would be 100 broken BMCs. |
| `max_parallel` | 10 | 50 | Updates running at once within a wave: the BMCs and the image server set the limit. |
| `soak` | 60 s | 30 min | After a wave, wait, then check its hosts again before the next wave: some faults show only after a while. |

With the lab's 10 BMCs in 3 racks: canary 2441 (fw image), wave 1 of 3 (one per rack), wave 2 of 6 (two per rack).

## Approach in production IT Equipment

![Production](docs/prod.svg)

![The lab today, on the production system's layout](docs/production-system-lab.svg)

What the lab implements, drawn where [the production system](#a-production-system---scaling-for-a-large-fleet) has
each part; an empty place is a part it doesn't have yet. `lab/images.sh` downloads OpenBMC's builds, records their
sha256 and approves the newer into `lab/images/`, the store. `make` is the API and CLI (`YES=1` approves), `pipeline.sh`
and `rollout.py` the workflow engine, the YAML files the catalog and inventory, the run records the rollout state.
`rollout.py run` is the site agent: it fetches each image from the nginx cache, checks it and pushes it to ten emulated
BMCs over Redfish, then resets, post-checks and, on a failure, rolls back. Every run's metrics go through the
Pushgateway to Prometheus and Grafana.

Equipment running production services runs the same tools, the same pipeline and the same six steps with `SITE=prod`; only the inventory, the credentials and the policy change.

Production (**ASUS**, **Dell**, **HPE**, **Gigabyte**, **Supermicro**) is **read-only**: `make collect SITE=prod`, the views (`make health SITE=prod`, `make firmware SITE=prod`...), `make plan SITE=prod`, `make dry-run SITE=prod`, and `exporter-prod`, every 5 minutes. `make update` and `make fault` refuse `SITE=prod`. They read only, plus a Redfish session login and logout. The Supermicro has no row in `prod/baseline.yaml` yet, so the plan skips it as not in the baseline.

[prod/inventory.example.yaml](prod/inventory.example.yaml)

```yaml
# prod/inventory.yaml
defaults: {scheme: https, verify: false}
servers:
  - {host: 10.0.0.5, vendor: Dell, project: cluster-a, rack: r1,
     username_env: DELL_REDFISH_USERNAME, password_env: DELL_REDFISH_PASSWORD}
```

```sh
# prod/.env, read by make
DELL_REDFISH_USERNAME=...
DELL_REDFISH_PASSWORD=...
```

How a production rollout is built, from the whole fleet down to one host:

1. **Rings, one at a time.** The lab's emulated BMCs first, then internal or spare nodes, then production region by
   region. A bad image that reaches every region at once can't be contained; the same pipeline runs at each ring.
2. **One component per pipeline**, in rollout order: the BMC first (it carries the other updates), then BIOS, then the
   rest. The baseline says the target per hardware model, and the catalog has the image for each.
3. **Group by what fails together.** The canary covers every hardware model the update touches, because firmware
   faults follow the model. Each wave spreads across racks, PDUs and InfiniBand pods, with at most `max_per_rack`
   hosts of one rack in a wave, so no rack or pod loses more capacity than it can spare. Idle and spare nodes go
   first; the scheduler drains busy ones (`--drain`) before a reset that restarts the host.
4. **Waves that grow, gates that start strict**, as [the rollout](#how-the-rollout-works) does everywhere, with
   production's policy: canary, then 5%, 25% and the rest; the first wave strict, later ones halting over 2%; 30
   minutes of soak between waves.
5. **Evidence.** The plan, the run records and the report in `prod/runs/` are the change's record: `report.json` for
   the services that track fleet state, the report for the people who approve the next ring.

To run it:

- **Accounts**: keep the poller on a ReadOnly role. Give the rollout its own account whose role can update firmware
  and reset the BMC (`ConfigureComponents` and `ConfigureManager`, Administrator on most BMCs).
- **Scope**: mark hosts `writable: true` only for the change window, give each its `rack`, and mark a few
  representative spare hosts per model `canary: true`.
- **Drain**: pass the scheduler's commands, e.g. `--drain "scontrol update NodeName={node} State=DRAIN Reason=firmware"`
  and `--undrain "scontrol update NodeName={node} State=RESUME"`. They run only when the reset restarts the host.
- **Images**: the vendor packages in `prod/images.yaml` with their sha256, and the packages of the versions running
  now, so every host has a way back. Serve them from the site's HTTPS cache and set `IMAGE_CACHE` to its address, as
  the lab does: `images.yaml`'s files become paths in it, and the rollout fetches, checks and pushes each one. A BMC
  that offers `SimpleUpdate` with the URL's protocol and target allowed can pull instead, given the image's full URL.
  The plan's Push and Pull columns show which, per resource, and pre-flight blocks a host whose method the BMC
  doesn't allow.
- **Run**: `make dry-run SITE=prod` is the dry run. The update itself, once hosts are `writable`, is
  `SITE=prod YES=1 ./pipeline.sh "Manager (BMC)"` with `prod/.env` exported, from an admin host: the Makefile never writes
  to production.

---
## Not built yet
- Rolling back by switching to the other A/B bank (a host that would need it stays drained as
  `needs_attention`).
- Rolling out ring by ring or region by region.

### A production system - scaling for a large fleet

![A production firmware-update system](docs/production-system.svg)

At fleet scale the rollout becomes a distributed system. The figure is the target, in four parts:

- **Supply chain**: a vendor image is downloaded, its checksum and signature checked, tested on lab hosts of every
  model, then promoted into an approved store (Artifactory or S3).
- **Control plane**: an operator requests a rollout through an API and CLI, and approves the canary. A workflow engine
  (Temporal, or Argo) reads the firmware catalog (the **baseline** per model) and the inventory (NetBox or Nautobot), pre-flights,
  plans the canary and waves, and applies the gates. A rollout state database (PostgreSQL) holds each host's state and
  wave, a lock per host so two rollouts never touch the same host, and an append-only audit trail; a gate is a query on
  it. It holds every host's state at every step, so a stopped rollout shows where each host was, and a rerun knows what
  is left. The engine keeps its own store too: its workflow history (each activity's inputs and outputs, the timers
  of a soak, the approval signals), which it replays to continue after a crash.
- **Each site**: a site agent, the Redfish worker that runs the update steps (`rollout.py run` here), takes the work of each wave, gets short-lived BMC credentials
  from Vault, drains hosts through the scheduler (Slurm or Kubernetes) and drives the update over Redfish. Images
  come from a cache in the site, so each file crosses the WAN once per site: a BMC that offers `SimpleUpdate` pulls
  it, and for a push-only BMC the agent reads it from the cache and pushes it. The poller isn't the agent: it runs
  beside it as the site's exporter, read-only, on its own ReadOnly account, so a bug in it can never write.
- **Observability**: the agents and the engine send events and metrics (Prometheus, Grafana); a halt or a quarantined
  host pages someone.

The numbered badges are the rollout's steps where they happen: 0 is ingest, before any rollout; 1 to 6 are the steps
every host goes through, as in [the rollout](#how-the-rollout-works). The BMC stays the only truth about what runs:
the database records intent and observations, and a host is read again over Redfish before anything acts on it.

**Firmware images: the catalog says what, the cache holds the bytes.** The firmware catalog lists, per hardware model
and component, the approved version with its file and sha256 (today `baseline.yaml` and `images.yaml`, reviewed in
git). An image gets in only after its checksum and signature are checked and it has passed on lab hosts of that model;
it is then promoted into the approved store, and each site's HTTPS cache serves it from there. Pull and push both read
the catalog and both take the bytes from the site's cache, never from a vendor at update time; they differ in who
downloads:

| | Pull: `SimpleUpdate` | Push: `MultipartHttpPushUri` |
|---|---|---|
| Who downloads the image | the BMC, from the cache URL the site agent hands it | the site agent, from the same cache |
| Checked before it is flashed | its sha256 when it was promoted; the vendor's signature by the BMC | its sha256 by the agent, against the catalog, before it pushes; the signature by the BMC |
| The path | cache → BMC | cache → agent → BMC |
| For | a BMC that allows the URL's protocol and target | a BMC that can't pull, like the lab's GB200 build |

Everything a rollout may install, the rollback's image of the running version too, is in the site's cache before the
canary starts. The lab runs the push column for real ([The lab stack](#the-lab-stack)).

The catalog and the caches meet on one key, the image's path. A catalog entry names its image by a path in the approved
store, best under its sha256 (`bmc/<sha256>.tar`), so a path can never serve other bytes; every site's cache serves the
store's files under the same paths. A site's configuration holds only its cache's address, so one catalog serves every
site: the agent joins the two, `https://cache.<site>/` and the entry's path, then pushes that file or hands that URL to
the BMC. Promotion writes the file first and the catalog entry after it, so the catalog never names an image a cache
can't serve.

**Tools for the inventory and the rollout state.**

| Store | What it holds | Tools |
|---|---|---|
| Inventory / CMDB: what should exist | every server: its BMC address, rack, PDU, model and role, and the rollout's own fields (`canary`, `writable` for a change window) | **NetBox** or **Nautobot**, open source: racks, power, cables and IPs, with REST and GraphQL APIs. The rollout reads a ring's hosts from the API instead of `inventory.yaml`. Nautobot adds Jobs, and an app that tracks validated software per device type, next to the baseline. ServiceNow CMDB or Device42 where the company already runs one |
| Rollout state: what is happening | each host's state and wave, a lock per host, every step's intent and result, each gate's decision | **PostgreSQL**: a lock per host (a row or advisory lock) so two rollouts never touch one host, the gate as a query, an append-only audit table, a step's evidence as JSONB. Today's run records map onto it one to one: an event is a row. The workflow engine keeps its own history beside it: **Temporal** on PostgreSQL or Cassandra, or **Argo Workflows** on Kubernetes |
| Observed state: what runs | the versions and health each BMC reports | the BMC itself, read over Redfish before anything acts on it; **Prometheus** keeps what the exporters read, **VictoriaMetrics** for a longer history |

How this repository maps onto it:

| In the system | In this repository today | Next |
|---|---|---|
| Approved store, site cache | the lab's image cache, nginx over HTTPS on `lab/images/`: `rollout.py` fetches each image into `lab/spool/`, checks its sha256 against `images.yaml` and pushes it. A URL in `images.yaml` is handed to `SimpleUpdate` where the BMC allows it | the approved store as object storage (S3, MinIO or Artifactory), and each site's cache an nginx caching proxy in front of it |
| Firmware catalog | `baseline.yaml` and `images.yaml`, reviewed in git | the same |
| Inventory / CMDB | `inventory.yaml`; the poller's snapshots are the observed versions, and `make firmware SITE=prod` shows drift from the baseline | generate the inventory from NetBox or Nautobot |
| Rollout API and CLI | the `make` targets and `pipeline.sh`; `YES=1` is the approval | an approval between the canary and wave 1 |
| Workflow engine | `pipeline.sh`'s stages and `rollout.py run`: waves, gates, soak | a service that resumes a stopped rollout |
| Rollout state DB | `<site>/runs/*.jsonl`: every step's intent before it and result after it; `report.json` | PostgreSQL, with host locks and the gate as a query |
| Engine store | none: a rerun plans again, and pre-flight skips the hosts already on the target | the engine's own |
| Site agent | `rollout.py run` on an admin host | one per site |
| Scheduler | the `--drain` and `--undrain` commands | the same |
| Secrets | `prod/.env` and `observability/grafana/.grafana.env`, git-ignored | Vault, short-lived credentials |
| Observability | the report: terminal, HTML and `report.json`; an exporter per site with every BMC's telemetry, health and firmware, the rollout's metrics pushed live, and Grafana dashboards in a folder per site ([Observability](#observability)) | paging the on-call on a halt or a host left for a person; an exporter and a Pushgateway in each site; Redfish `EventService` subscriptions, so a fault arrives in seconds rather than at the next read |


## Layout

```
Makefile            every command: make help
pipeline.sh         plan → canary → waves → report
poller.py           collect, the views (health, firmware, inventory, telemetry, capabilities, diff) and the exporter
redfish.py          the Redfish connection both tools share: session, token, GET, POST, PATCH
rollout.py          plan, run, report
test_rollout.py     the rollout's decisions on recorded Redfish data: make test
mutations.py        breaks rollout.py one rule at a time; each break must fail a test: make mutations
fixtures/           the lab BMC as a collect saved it, which the tests read
constants.py        what to crawl, table columns, rollout constants
helpers.py          shared helpers
schemas/            DMTF Redfish JSON Schemas for sensor units, downloaded on first use (git-ignored)
lab/                the emulated BMCs: Dockerfile, docker-compose.yaml, images.sh, heal.sh, scenario.sh, inventory.yaml,
                    rollout.yaml; cache/, the image cache's nginx config
prod/               the real fleet: baseline.yaml, rollout.yaml; inventory.yaml and .env stay local
observability/      Compose for the exporters, Pushgateway, Prometheus and Grafana; a Lab and a Prod dashboard folder;
                    grafana/grafana.ini; grafana/.grafana.env, Grafana's admin account, stays local;
                    grafana/prod_dashboards.py makes the prod dashboards from the lab ones
docs/               diagrams: the rollout, the lab, production today, a production system
docs/runs/          reports of real lab runs: the update and every bad-update scenario
```
