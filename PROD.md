# Production

The real fleet, ASUS, Dell, HPE, Gigabyte and Supermicro, has only been read: collects, plans, dry runs and the
exporter, never an update. This page covers how a production rollout is built and run, the exporter on the real
fleet, the update as Redfish resources, the fleet in Grafana, what isn't built yet, and the system the rollout grows
into at fleet scale, with how this repository maps onto it. Every update so far ran on [the lab](LAB.md), which also
has [the rollout step by step](LAB.md#how-the-rollout-works).

## Current production setup

![Production](docs/prod-stack.svg)

`SITE=prod` selects the real fleet's inventory, credentials and policy. The current setup uses the read-only
commands; the six-step update flow has been exercised only on the lab's BMCs.

Production (**ASUS**, **Dell**, **HPE**, **Gigabyte**, **Supermicro**) has been used **read-only**: `make collect SITE=prod`, the views (`make health SITE=prod`, `make firmware SITE=prod`...), `make plan SITE=prod`, `make dry-run SITE=prod`, and `exporter-prod`, configured with a 30-second polling interval. They read only, plus a Redfish session login and logout. `make update` and `make fault` refuse `SITE=prod`, and no production host is marked `writable`. The runner underneath can write: `rollout.py run --yes` updates a host the inventory marks `writable: true`, which is how a change window would run ([Before enabling production writes](#before-enabling-production-writes)). The Supermicro has no row in `prod/baseline.yaml` yet, so the plan skips it as not in the baseline.

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

The proposed production rollout, from the whole fleet down to one host (rings and scheduler integration are not
implemented here):

1. **Rings, one at a time.** The lab's emulated BMCs first, then internal or spare nodes, then production region by
   region. A bad image that reaches every region at once can't be contained; the same pipeline runs at each ring.
2. **One component per pipeline**, in rollout order: the BMC first (it carries the other updates), then BIOS, then the
   rest. The baseline says the target per hardware model, and the catalog has the image for each.
3. **Group by what fails together.** The canary covers every hardware model the update touches, because firmware
   faults follow the model. Each wave spreads across racks, with at most `max_per_rack` hosts of one rack in a wave,
   which limits concurrent disruption by host count, not measured spare capacity. The canary prefers this limit but
   can exceed it when a model has no candidate in another rack; PDUs, InfiniBand pods and capacity need additional
   admission checks, which are not built.
   Idle and spare nodes go first; the scheduler drains busy ones before a reset that restarts the host.
4. **Waves that grow, gates that start strict**, as [the rollout](LAB.md#how-the-rollout-works) does everywhere, with
   production's policy: canary, then 5%, 25% and the rest; the first wave strict, later ones halting over 2%; 30
   minutes of soak between waves.
5. **Evidence.** The plan, the run records and the report in `prod/runs/` are the change's record: `report.json` for
   the services that track fleet state, the report for the people who approve the next ring.

### Before enabling production writes

- **Accounts**: keep the poller on a ReadOnly role. Give the rollout its own account whose role can update firmware
  and perform the required reset; verify the privileges on each BMC. The local inventory's `writable` flag is an
  application gate, not proof of the account's Redfish role.
- **Scope**: mark hosts `writable: true` only for the change window, give each its `rack`, and mark a few
  representative spare hosts per model `canary: true`.
- **Scheduler hooks**: `drain` and `undrain` are not configured in `prod/rollout.yaml`, and no integration with a real
  scheduler has been validated here. Before a host-reset update, supply and test commands using `{node}`; the drain
  must return only after the node is empty, its jobs ended. Without a drain, pre-flight blocks updates whose reset
  restarts the host (BIOS, system firmware); a BMC-only reset needs none.
- **Images**: the vendor packages in `prod/images.yaml` with their sha256, and the packages of the versions running
  now, so every host has a way back. Serve them from the site's HTTPS cache and set `IMAGE_CACHE` to its address, as
  the lab does: `images.yaml`'s files become paths in it, and the rollout fetches, checks and pushes each one. A BMC
  that offers `SimpleUpdate` with the URL's protocol and target allowed can pull instead, given the image's full URL.
  The plan's Push and Pull columns show which, per resource, and pre-flight blocks a host whose method the BMC
  doesn't allow.
- **Run**: `make dry-run SITE=prod` makes no firmware changes; it still saves the plan and stage HTML files and can
  send the plan and its pre-flight checks to Loki when `LOKI` is set. The update itself, once hosts are `writable`, is
  `SITE=prod YES=1 ./pipeline.sh "Manager (BMC)"` with `prod/.env` exported, from an admin host: the Makefile never writes
  to production. The plan is the approval: its stages refuse to run when an input file changed since, and block a host
  whose BMC isn't the one the plan read.

## The exporter on the real fleet

Judge host health from the `ComputerSystem` resource's `Status.HealthRollup` (or `Status.Health`) and inspect its
`Status.State`, alongside the Manager's own status. An HTTP 200 from `/redfish/v1` only confirms service availability.
The rollout already checks health on both discovered resource types before an update and compares it afterward.
The BMC and its Redfish service must be reachable to read the System resource; the host can be off or unhealthy
while its BMC continues to answer. See [availability and resource health](LAB.md#availability-and-resource-health).

What the exporter does differently from a one-off `collect`, because it never stops:

- **One Redfish session per BMC, reused while valid.** A login per read adds entries to the BMC's own logs. The
  exporter logs out after a read when the reported `SessionService.SessionTimeout` is shorter than the configured
  polling interval. At the current 30-second interval, a timeout of exactly 30 seconds does not meet that condition;
  a session that expires gets a new login when the next read returns 401. On shutdown it logs out of held sessions.
- **A refused login stops that host.** A 401 on a fresh login is not retried until a restart, since BMCs lock the
  account after a few failures; `redfish_login_refused` says so.
- **Prod targets a read every 30 seconds.** [Compose](observability/docker-compose.yaml) sets `--interval 30`;
  Prometheus scrapes the exporter every 10 seconds. Each BMC has its own loop: if a crawl takes longer than the
  interval, the next starts immediately after it. The crawl budget is three intervals (90 seconds here), or a
  shorter inventory deadline, checked between requests. A full iDRAC 8 crawl previously took about two minutes, so
  this setting can produce partial reads and leave little idle time. Reassess the interval against complete crawl
  times before expanding the fleet. A scrape serves the latest finished read; it does not trigger a new crawl.
- **Units and thresholds from the standards.** A reading's unit comes from the BMC's `MetricDefinition`, else the
  DMTF schema of its resource (`ReadingCelsius` is `Cel`), else its `ReadingUnits`, else the unit DMTF's newest Sensor
  schema gives its `ReadingType` (a Gigabyte's v1.0 Sensors report none). Thresholds take the Sensor schema's names
  (`UpperThresholdNonCritical` in Thermal is `UpperCaution`); a 0 is none set (iLO 4, and AMI's lower thresholds).
- **No task queue.** Prometheus schedules the scrapes and the exporter its reads. Celery would add a broker, workers
  and a beat scheduler for one periodic read; durable, resumable jobs, like a rollout, belong to the workflow engine
  of [a production system](#a-production-system---scaling-for-a-large-fleet).

### Future work: BMC events into Loki

Today the events in Loki come from `rollout.py`, as [the lab's event flow](LAB.md#how-the-lab-sends-events-to-loki)
describes. Receiving hardware alerts independently of a rollout needs a separate event receiver; this integration
has not been implemented or tested on the production fleet.

1. Discover each BMC's `EventService` and the delivery methods it supports. For HTTP event delivery, create an
   `EventDestination` subscription in its `Subscriptions` collection, with the receiver's HTTPS URL as `Destination`.
   Creating a subscription writes BMC configuration; it is outside the current read-only production setup.
2. The BMC posts an `Event` payload to the receiver. Preserve its `Events` entries and available fields such as
   `MessageId`, `Severity`, `EventTimestamp` and `OriginOfCondition`, together with the source BMC.
3. The receiver converts those events into Loki log entries and sends them to `POST /loki/api/v1/push`. Loki itself
   does not create Redfish subscriptions or receive Redfish payloads directly in this design.
4. Validate delivery on each model, monitor the receiver and subscription state, and handle retries and duplicate
   deliveries. Keep the exporter polling: event delivery complements the current health readings.

The proposed flow is `BMC EventService → event receiver → Loki → Grafana`. DMTF's
[event delivery specification](https://www.dmtf.org/sites/default/files/standards/documents/DSP0266_1.17.1.html#eventing)
defines subscriptions and notifications; its [Redfish Event Listener](https://github.com/DMTF/Redfish-Event-Listener)
is a starting point for evaluating a receiver.

## The update, resource by resource

The diagram is the target production design, including capability-dependent flows that the current runner does
not implement. It is not evidence of production updates; [Not built yet](#not-built-yet) lists the gaps.

![The production rollout as Redfish resources: plan, pre-flight, drain, update, activation, reset, verify, recover](docs/prod-rollout.svg)

## The production fleet in Grafana

The Prod folder over two days of the real fleet, read-only. The fleet manager shows which BMCs answer, their health,
equipment and firmware, and how each component can be updated; the host dashboard shows one host in full: its
firmware, the health of every component, and every reading against its thresholds.

### Fleet manager

![Prod · Fleet manager](docs/grafana/prod/fleet.png)

![Prod · Fleet manager: firmware and the readings nearest their thresholds](docs/grafana/prod/fleets-details-2.png)

![Prod · Fleet manager: what is not OK, and crawl times](docs/grafana/prod/fleet-details-1.png)

### Host

![Prod · Host: a Supermicro SYS-221H-TNR](docs/grafana/prod/supermicro.png)

![Prod · Host: thermal and power](docs/grafana/prod/supermicro-details.png)

![Prod · Host: the BMC and its Redfish service](docs/grafana/prod/supermicro-details-2.png)

## Not built yet

- **Production activation validation**: the runner requests `OnReset`; it does not negotiate apply times or drive a
  Task-to-Job handoff. Validate the supported apply time, reset and recovery procedure per model before enabling
  writes. Only the lab's OpenBMC update path has been exercised end to end.
- **Complete promotion checks**: blocked/skipped hosts are excluded from the failure-rate denominator, so a passed
  gate does not prove every planned host updated. A soak failure halts but does not drain again or roll back;
  workload health and capacity checks need a real scheduler and host integration.
- **Other update flows**: switching to the other A/B bank as a rollback (pre-flight doesn't count a bank as a way
  back), an `Activate` action, a staged image, an apply scheduled as a Job. The rollout uses an upload applied at
  reset, its own reset, and a reinstall of the version before as the way back.
- **Durable orchestration**: a lock per host, so two rollouts never touch one; picking up a stopped run where it
  was, after a crash; resolving a request whose answer was lost (an update the BMC may or may not have taken). The run
  records say what happened, but nothing resumes from them: a stopped pipeline is planned and run again, and
  pre-flight skips the hosts already on the target. In production this is the workflow engine and the rollout state
  database ([a production system](#a-production-system---scaling-for-a-large-fleet)).
- **Limits beyond racks**: per PDU, fabric pod or remaining capacity.
- **Pull integrity**: a pulled image is never seen by the agent, so its sha256 isn't checked against the catalog;
  it requires a trusted image URL, enforced store immutability and a validated BMC signature check. The lab store
  does not enforce immutability, and the lab BMCs do not support pull.
- Rolling out ring by ring or region by region.
- **A rollout API**: an operator starts and approves a rollout only from the CLI (`make`, `YES=1`).
- **BMC events into Loki**: an event receiver, Redfish subscriptions and forwarding of `Event` payloads, validated
  per model; see [the proposed event flow](#future-work-bmc-events-into-loki).

## A production system - scaling for a large fleet

![A production firmware-update system](docs/prod-solution-architecture.svg)

At fleet scale the rollout becomes a distributed system. The figure is the target, in four parts:

- **Supply chain**: a vendor image is downloaded, its checksum and signature checked, tested on lab hosts of every
  model, then promoted into an approved store (S3 or Artifactory).
- **Control plane**: an operator requests a rollout through an API and CLI, and approves the canary. A workflow engine
  (Temporal, or Argo) reads the firmware catalog (the **baseline** per model) and the inventory (NetBox or Nautobot), pre-flights,
  plans the canary and waves, and applies the gates. A rollout state database (PostgreSQL) holds each host's state and
  wave, a lock per host so two rollouts never touch the same host, and an append-only audit trail; a gate is a query on
  it. It holds every host's state at every step, so a stopped rollout shows where each host was, and a rerun knows what
  is left. The engine keeps its own store too: its workflow history (each activity's inputs and outputs, the timers
  of a soak, the approval signals), which it replays to continue after a crash.
- **Each site**: a site agent, the Redfish worker that runs the update steps (`rollout.py run` here), takes the work
  of each wave, gets BMC credentials from Vault, drains hosts through a validated scheduler integration and drives the update over Redfish. Images
  come from a cache in the site, reducing repeated WAN transfers while entries remain cached: a BMC that offers
  `SimpleUpdate` pulls it, and for a push-only BMC the agent reads it from the cache and pushes it. The poller isn't the agent: it runs
  beside it as the site's exporter on an account restricted to ReadOnly operations. [Vault KV](https://developer.hashicorp.com/vault/docs/secrets/kv) can store BMC passwords;
  automatic rotation or short-lived BMC accounts require a separate, validated integration, not just installing Vault.
- **Observability**: the agents and the engine send metrics to Prometheus and rollout events to Loki. A separate
  event receiver would subscribe to each BMC's Redfish `EventService` and forward its notifications to Loki
  ([future work](#future-work-bmc-events-into-loki)); Grafana reads both stores, and a halt or a quarantined host would
  page someone through Alertmanager.

The numbered badges are the rollout's steps where they happen: 0 is ingest, before any rollout; 1 to 6 are the steps
every host goes through, as in [the rollout](LAB.md#how-the-rollout-works). The BMC stays the only truth about what runs:
the database records intent and observations, and a host is read again over Redfish before anything acts on it.

**Firmware images: the catalog says what, the cache holds the bytes.** The firmware catalog lists, per hardware model
and component, the approved version with its file and sha256 (today `baseline.yaml` and `images.yaml`). Only
`prod/baseline.yaml` is tracked here; the lab catalog and baseline are generated and git-ignored. A production
approval process must version and review its catalog. In the target design an image gets in only after its checksum
and signature are checked and it has passed on lab hosts of that model;
it is then promoted into the approved store, and each site's HTTPS cache serves it from there. Pull and push both read
the catalog and both take the bytes from the site's cache, never from a vendor at update time; they differ in who
downloads:

| | Pull: `SimpleUpdate` | Push: `MultipartHttpPushUri` |
|---|---|---|
| Who downloads the image | the BMC, from the cache URL the site agent hands it | the site agent, from the same cache |
| Checked before it is flashed | its sha256 when it was promoted; the vendor's signature by the BMC. Nothing compares what the BMC downloads with the catalog | its sha256 by the agent, against the catalog, before it pushes; the signature by the BMC |
| What keeps it the approved image | a digest-addressed URL plus enforced immutability/write restrictions in the store; the path alone does not enforce it | the agent's check: a copy that doesn't match is deleted, never pushed |
| The path | cache → BMC | cache → agent → BMC |
| For | a BMC that allows the URL's protocol and target | a BMC that can't pull, like the lab's GB200 build |

The target design should prewarm and verify every required image, including rollback images, before the canary.
Today pre-flight fetches required push images on demand and verifies the local copies; it does not prewarm pull URLs.
The lab runs the push column for real ([The lab stack](LAB.md#the-lab-stack)).

The catalog and the caches meet on one key, the image's path. A catalog entry names its image by a path in the approved
store, preferably under its sha256 (`bmc/<sha256>.tar`), with storage policy preventing replacement; every
site's cache serves the store's files under the same paths. A site's configuration holds only its cache's address, so one catalog serves every
site. The current runner joins `IMAGE_CACHE` with relative paths for push; pull entries must already contain the full
cache URL. Constructing site-specific pull URLs from a shared catalog is future work. Promotion writes the
file first and the catalog entry after it; cache and store availability must still be checked before execution.

**Tools for the inventory and the rollout state.**

| Store | What it holds | Tools |
|---|---|---|
| Inventory / CMDB: what should exist | every server: its BMC address, rack, PDU, model and role, and the rollout's own fields (`canary`, `writable` for a change window) | **NetBox** or **Nautobot**, open source: racks, power, cables and IPs, with REST and GraphQL APIs. The rollout reads a ring's hosts from the API instead of `inventory.yaml`. Nautobot adds Jobs, and an app that tracks validated software per device type, next to the baseline. ServiceNow CMDB or Device42 where the company already runs one |
| Rollout state: what is happening | each host's state and wave, a lock per host, every step's intent and result, each gate's decision | **PostgreSQL**: a lock per host (a row or advisory lock) so two rollouts never touch one host, the gate as a query, an append-only audit table, a step's evidence as JSONB. Today's run records map onto it one to one: an event is a row. The workflow engine keeps its own history beside it: **Temporal** on PostgreSQL or Cassandra, or **Argo Workflows** on Kubernetes |
| Observed state: what runs | the versions and health each BMC reports | the BMC itself, read over Redfish before anything acts on it; **Prometheus** keeps what the exporters read, **VictoriaMetrics** for a longer history |

How this repository maps onto it:

| In the system | In this repository today | Next |
|---|---|---|
| Approved store, site cache | the lab's store, a SeaweedFS bucket populated by the signature-checking ingest script, and nginx over HTTPS caching it: `rollout.py` fetches each image into `lab/spool/`, checks its sha256 against `images.yaml` and pushes it. A URL in `images.yaml` is handed to `SimpleUpdate` where the BMC allows it | the approved store on S3 or Artifactory, and the same nginx cache in each site |
| Firmware catalog | `prod/baseline.yaml` is tracked; lab baseline and images catalog are generated and git-ignored | version and review the approved catalog |
| Inventory / CMDB | `inventory.yaml`; the poller's snapshots are the observed versions, and `make firmware SITE=prod` shows drift from the baseline | generate the inventory from NetBox or Nautobot |
| Rollout API and CLI | the CLI only: the `make` targets and `pipeline.sh`; `YES=1` is the approval | an API, and an approval between the canary and wave 1 |
| Workflow engine | `pipeline.sh`'s stages and `rollout.py run`: waves, gates, soak | a service that resumes a stopped rollout |
| Rollout state DB | `<site>/runs/*.jsonl`: every step's intent before it and result after it; `report.json`. A record: no host locks, no resume | PostgreSQL, with host locks and the gate as a query |
| Engine store | none: a rerun plans again, and pre-flight skips the hosts already on the target | the engine's own |
| Site agent | `rollout.py run` on an admin host | one per site |
| Scheduler | Optional `drain` and `undrain` hooks; neither site configures them, and no integration with a real scheduler has been validated | implement and validate the site's commands, including waiting for the node to become empty |
| Secrets | `prod/.env` and `observability/grafana/.grafana.env`, git-ignored | Vault-backed storage; validate account rotation separately |
| Observability | the report: terminal, HTML and `report.json`; an exporter per site with every BMC's telemetry, health and firmware, the rollout's metrics pushed live, its events in Loki, and Grafana dashboards in a folder per site ([Observability](LAB.md#observability)) | paging the on-call on a halt or a host left for a person; an exporter and a Pushgateway in each site; an event receiver that subscribes to Redfish `EventService` and forwards BMC events to Loki |
