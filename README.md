# Redfish firmware rollout

BMC firmware updates across a server fleet over the standard [Redfish](https://www.dmtf.org/standards/redfish) API:
pre-flight, a canary, then waves behind gates, and a rollback when a host fails its post-check. Standard Redfish
only, no vendor Oem code.

| Site | What it is | Status |
|---|---|---|
| **Lab**, [LAB.md](LAB.md) | 10 emulated OpenBMC BMCs (QEMU in Docker), an image store fed by a signature-checking ingest script, an HTTPS image cache | every update so far ran here |
| **Production**, [PROD.md](PROD.md) | ASUS, Dell, HPE, Gigabyte and Supermicro servers | read-only: monitoring, plans, dry runs |

`make update` refuses production; `rollout.py` writes only to hosts marked `writable: true`, and only with `--yes`.

![The lab rollout: plan, canary and waves on the ten emulated BMCs, step by step](docs/lab-rollout.svg)

## Components

| Part | What it does |
|---|---|
| [rollout.py](rollout.py), [pipeline.sh](pipeline.sh) | Plans and runs a rollout, and reports it |
| [poller.py](poller.py) | Reads every BMC (health, firmware, inventory, sensors) into views, or serves it as Prometheus metrics. Read-only |
| [lab/](lab/) | The emulated BMCs, the image store (SeaweedFS) and the image cache (nginx) |
| [observability/](observability/docker-compose.yaml) | Exporters, Pushgateway, Loki, Prometheus, Grafana |

## The rollout

[pipeline.sh](pipeline.sh) runs four stages:

1. **plan**: reads every BMC live, pre-flights it (go, skip or block) and puts the hosts in waves. `plan.json` freezes
   the inputs and each BMC's identity; a later stage refuses a changed input.
2. **canary**: a host per hardware model. Any failure halts.
3. **waves**: growing, spread across racks, a soak between them. The first halts on any failure, later ones over
   `halt_at`.
4. **report**: terminal, HTML and `report.json` after execution or a blocked plan; a successful dry run saves
   stage previews only.

Every host goes through six steps; the hosts of a wave run in parallel:

| # | Step | |
|---|---|---|
| 1 | Pre-flight | again, on a fresh read; verify the BMC's identity and the System and Manager resources' health |
| 2 | Drain | only when the reset restarts the host |
| 3 | Update | push, or `SimpleUpdate`, applied on reset; the Task followed to its end |
| 4 | Reset | graceful first, then wait until the BMC answers |
| 5 | Post-check | the target version, health no worse, no new failed task; on a failure, reinstall the version before |
| 6 | Undrain | a host that can't be recovered stays drained: `needs_attention` |

Each step is recorded before and after it runs, in `<site>/runs/`. Detail: [LAB.md](LAB.md#how-the-rollout-works).

## A real run

All ten lab BMCs: the canary, then waves of 3 and 6, every gate passed.

![The report of an update of all ten BMCs](docs/runs/update.svg)

Each bad update (a refused image, a failed post-check, a BMC that doesn't come back, a wrong checksum, a mix) stops at
a gate: [LAB.md](LAB.md#bad-updates).

## Quick start

Needs Linux x86-64, Docker with Compose, Python 3.10+, `curl` and `openssl`, and Grafana's admin account in
`observability/grafana/.grafana.env` (git-ignored):

```sh
GRAFANA_ADMIN_USERNAME=...
GRAFANA_ADMIN_PASSWORD=...
```

```sh
make setup        # venv
make test         # the unit tests, no BMC needed
make lab-up       # the image store, OpenBMC's two newest builds, 10 BMCs (~10 min)
make monitor-up   # exporters, Pushgateway, Loki, Prometheus, Grafana on http://127.0.0.1:3000
make collect      # read every BMC; then make health, make firmware, ...
make dry-run      # plan, canary, waves; no firmware changes
make update       # a canary of 1 BMC, then waves of 3 and 6
make report       # the latest pipeline's verdict
make lab-reset    # stop the lab and wipe the BMCs' flash
make lab-up       # boot the older build again; the image store is preserved
```

A dry run still reads the BMCs, writes `plan.json` and the stages' HTML files, and can publish the plan and its
pre-flight checks to Loki when `LOKI` is set. It previews the updates without applying firmware changes.

`make help` lists every target. `SITE=prod` reads the real fleet; `make update` and `make fault` refuse it. To roll
the lab back, swap the commented line in `lab/baseline.yaml` and run `make update` again.

## Tests

| Command | What it checks |
|---|---|
| `make test` | 22 tests, no BMC: pre-flight, post-check, rollback, gates, the frozen plan, the ingest gate, the image cache, on recorded Redfish data. `make update` runs it first |
| `make mutations` | breaks 27 safety rules one at a time; every break fails a test |
| `make fault SCENARIO=…` | the real pipeline on the lab, with a fault injected |

## Not built yet

- An A/B bank switch as the rollback, `Activate`, staged images, Jobs
- Host locks, and resuming a stopped run: a stopped pipeline is planned again
- Limits per PDU, fabric pod or capacity; rolling out ring by ring
- A sha256 check of an image a BMC pulls
- A rollout API: the CLI only

Detail, and the production system it leads to: [PROD.md](PROD.md#not-built-yet).

## Layout

```
LAB.md              the lab, and the rollout in detail: steps, policy, runs, reports, tests, Grafana
PROD.md             the real fleet, a production rollout, what isn't built, the system at fleet scale
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
docs/               the diagrams: the rollout, the lab, production today, a production system, the lab on its
                    layout, one update as resources; grafana/, a screenshot of every panel
docs/runs/          reports of real lab runs: the update and every bad-update scenario
```
