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
| [control/](control/docker-compose.yaml) | The control plane, one per fleet: the ingest and the approved store (SeaweedFS), NetBox, the firmware catalog (YAML in git), the rollout state (PostgreSQL: a lock per host, every event of every run), Prometheus, Pushgateway, Loki, Grafana |
| [site/](site/docker-compose.yaml) | A site, one per site: the exporter, a Prometheus agent that pushes to the control plane, the HTTPS image cache (nginx). It only connects out |
| [lab/](lab/) | The lab's hardware, 10 emulated BMCs, and the lab site's settings |

## The rollout

[pipeline.sh](pipeline.sh) runs four stages:

1. **plan**: reads every BMC live, pre-flights it (go, skip or block) and puts the hosts in waves. `plan.json` freezes
   the inputs and each BMC's identity; a later stage refuses a changed input.
2. **canary**: a host per hardware model. Any failure halts.
3. **waves**: growing, spread across racks, a soak between them. The first halts on any failure, later ones over
   `failure_rate`.
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

A stage first takes the lock of every host it will touch, in the state database, or none: one pipeline at a time
touches a host. A host left `needs_attention` keeps its lock until a person runs `make unlock`.

## A real run

All ten lab BMCs: the canary, then waves of 3 and 6, every gate passed.

![The report of an update of all ten BMCs](docs/runs/update.svg)

Each bad update (a refused image, a failed post-check, a BMC that doesn't come back, a wrong checksum, a mix) stops at
a gate: [LAB.md](LAB.md#bad-updates).

## Quick start

Needs Linux x86-64, Docker with Compose, Python 3.10+, `curl` and `openssl`, and Grafana's admin account in
`control/observability/grafana/.grafana.env` (git-ignored):

```sh
GRAFANA_ADMIN_USERNAME=...
GRAFANA_ADMIN_PASSWORD=...
```

```sh
make setup        # venv
make test         # the unit tests, no BMC needed
make lab-up       # the control plane, the lab site, OpenBMC's two newest builds, 10 BMCs (~10 min)
make collect      # read every BMC; then make health, make firmware, ...
make dry-run      # plan, canary, waves; no firmware changes
make update       # a canary of 1 BMC, then waves of 3 and 6
make report       # the latest pipeline's verdict
make lab-reset    # stop the lab and wipe the BMCs' flash
make lab-up       # boot the older build again; the image store is preserved
```

A dry run still reads the BMCs, writes `plan.json` and the stages' HTML files, and can publish the plan and its
pre-flight checks to Loki when `LOKI` is set. It previews the updates without applying firmware changes.

Grafana is on http://127.0.0.1:3000. `make help` lists every target. `SITE=prod` reads the real fleet, and
`make site-up SITE=prod` starts its exporter and agent; `make update` and `make fault` refuse it. To roll the lab back,
swap the commented line in `control/catalog/lab/baseline.yaml` and run `make update` again.

## Tests

| Command | What it checks |
|---|---|
| `make test` | 26 tests, no BMC: pre-flight, post-check, rollback, gates, the frozen plan, the ingest gate, the image cache, on recorded Redfish data; host locks and append-only events, on the state database. `make update` runs it first |
| `make mutations` | breaks 31 safety rules one at a time; every break fails a test |
| `make fault SCENARIO=…` | the real pipeline on the lab, with a fault injected |

## Not built yet

- An A/B bank switch as the rollback, `Activate`, staged images, Jobs
- Resuming a stopped run: a stopped pipeline is planned again, once its hosts are checked and unlocked
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
tests/              test_rollout.py, the rollout's decisions on recorded Redfish data: make test;
                    mutations.py, breaks rollout.py one rule at a time, each break must fail a test: make
                    mutations; testdata/, the lab BMC as a collect saved it, which the tests read
constants.py        what to crawl, table columns, rollout constants
helpers.py          shared helpers
schemas/            DMTF Redfish JSON Schemas for sensor units, downloaded on first use (git-ignored)
control/            the control plane, Compose for one per fleet: ingest/, download-fw-images.sh and
                    promote-fw-images.sh with openbmc-dev.pub, the gate's pinned key; store/, the store's read-only
                    access; netbox/seed.py, which loads lab/inventory.yaml into NetBox once; state/schema.sql: events,
                    host_state, host_locks and the accounts redfish_rollout_sa and grafana; observability/,
                    Prometheus, Loki and Grafana: a Lab and a Prod dashboard folder, prod_dashboards.py making the prod
                    ones from the lab's; catalog/<site>/, the firmware catalog: baseline.yaml, images.yaml. The
                    secrets, .netbox.env, .state.env and .grafana.env, stay local
site/               a site, Compose for one per site: the exporter's Dockerfile, the Prometheus agent, cache/, the
                    image cache's nginx config
lab/                the lab: its hardware, the BMCs' Dockerfile and docker-compose.yaml, and scenario.sh; its site,
                    inventory.yaml, policy.yaml, prometheus-agent.yml
prod/               the real fleet's site: policy.yaml, prometheus-agent.yml; inventory.yaml and .env stay local
docs/               the diagrams: the rollout, the lab, production today, a production system, the lab on its
                    layout, one update as resources; grafana/, a screenshot of every panel; netbox/, the
                    lab's inventory in NetBox
docs/runs/          reports of real lab runs: the update and every bad-update scenario
```
