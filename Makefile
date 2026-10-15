# Redfish firmware tooling: `make` lists every target. The parts:
#   poller.py       reads the resources each BMC's Redfish service exposes: health, firmware, inventory, telemetry
#   rollout.py      updates firmware: pre-flight, a canary, then waves, each after a gate (pipeline.sh runs the stages)
#   control/        the control plane, one per fleet: NetBox, the state database, the approved store, observability
#   site/           a site, one per site: the exporter, a Prometheus agent, the image cache; it connects out only
#   lab/            the lab's hardware: 10 emulated OpenBMC BMCs, QEMU in Docker, the only BMCs the rollout updates
# Every target runs on the lab unless SITE=prod: the real fleet in prod/, read-only.

SITE      ?= lab
PY        := venv/bin/python
VIEWS     := health firmware inventory telemetry capabilities diff
WIDTH     := 200
CONTROL   := docker compose -f control/docker-compose.yaml
SITE_STACK = docker compose -p redfish-site-$(SITE) -f site/docker-compose.yaml
LAB       := docker compose -f lab/docker-compose.yaml
# the tools' read-only NetBox token, from control/netbox/.netbox.env
TOKEN      = NETBOX_TOKEN=$$(sed -n 's/^NETBOX_TOKEN=//p' control/netbox/.netbox.env 2>/dev/null)
# the rollout state database, where an update locks its hosts and adds its events, as the account redfish_rollout_sa
DB         = STATE_DB=postgresql://redfish_rollout_sa:$$(sed -n 's/^REDFISH_ROLLOUT_SA_PASSWORD=//p' control/state/.state.env \
             2>/dev/null)@127.0.0.1:5432/rollout
# Grafana's read-only account on it
STATE_RO   = STATE_GRAFANA_PASSWORD=$$(sed -n 's/^GRAFANA_PASSWORD=//p' control/state/.state.env 2>/dev/null)
# the firmware dry-run and update work on
FIRMWARE   = $(or $(COMPONENT),Manager (BMC))

ifeq ($(SITE),lab)
  # OpenBMC's public default account; the rollout pushes its metrics to the Pushgateway and its events to Loki
  ENV       := SITE=lab LAB_USERNAME=root LAB_PASSWORD=0penBmc PUSHGATEWAY=http://127.0.0.1:9091 \
               LOKI=http://127.0.0.1:3100 IMAGE_CACHE=https://127.0.0.1:8443 $(TOKEN) $(DB)
  # pre-flight can't put OpenBMC versions in order, so the lab allows either direction
  PLAN_ARGS := --allow-downgrade
  # the lab site: its exporter on :9101, its Prometheus agent on :9201, and the image cache, as it updates firmware
  SITE_ENV  := EXPORTER_PORT=9101 INTERVAL=10 AGENT_PORT=9201 COMPOSE_PROFILES=cache
else ifeq ($(SITE),prod)
  # the BMC accounts, from prod/.env
  ENV       := set -a; [ ! -f prod/.env ] || . prod/.env; set +a; SITE=prod
  # the production site: its exporter on :9102, its Prometheus agent on :9202; no cache, as it is only read
  SITE_ENV  := EXPORTER_PORT=9102 INTERVAL=30 AGENT_PORT=9202
else
  $(error SITE is lab or prod, not "$(SITE)")
endif
LAB_ONLY = $(if $(filter lab,$(SITE)),,$(error make $@ writes firmware, so it runs on the lab only: prod is read-only))

define HELP
Usage: make <target> [SITE=prod] [HOST=...] [COMPONENT="..."] [ARGS="..."]
Every target runs on the lab unless SITE=prod: the real fleet in prod/, which is only ever read.

Setup
  make setup               create venv/ and install requirements.txt
  make test                the rollout's decisions on recorded Redfish data, in about a second, no BMC needed; the
                           host locks' test needs the state database, else it's skipped
  make mutations           break each safety rule of rollout.py, in a copy: each break must fail a test

Control plane: one per fleet, every port on 127.0.0.1; the sites connect to it, never it to them
  make control-up          start NetBox (http://127.0.0.1:8000, admin's password: control/netbox/.netbox.env), the state
                           database (127.0.0.1:5432; its passwords: control/state/.state.env), the approved store,
                           Prometheus, the Pushgateway, Loki and Grafana (http://127.0.0.1:3000, account:
                           control/observability/grafana/.grafana.env). Load lab/inventory.yaml's servers into NetBox,
                           once, and ingest OpenBMC's two newest builds into the store when the catalog is missing
                           (download, check their signatures, upload)
  make control-down        stop it; its data stays in volumes
  make state               every host's lock, then each host's latest state
  make unlock HOST=...     give back a host's lock once a person checked it; several: HOST="a b"

Site: one per site, SITE=lab (the default) or prod; every connection goes out, to the control plane
  make site-up             start its exporter and Prometheus agent, which pushes to the control plane's Prometheus;
                           on the lab also its HTTPS image cache on :8443
  make site-logs           follow its exporter's and agent's logs
  make site-down           stop it

Lab: 10 emulated OpenBMC BMCs, QEMU in Docker, on 127.1.0.1-10
  make lab-up              start the control plane and the lab site, then boot the BMCs (about 10 min)
  make lab-down            stop them; each keeps its flash
  make lab-reset           stop them and wipe their flash: they boot the older build again; the store keeps its images
  make lab-clean           delete the BMCs' flash and the lab's catalog: lab-up then ingests the newest builds again

Poller: reads every BMC's Redfish resources, never writes
  make collect             crawl every BMC into <site>/snapshots/, which the views read
  make health              health per host, then everything not OK, jobs to check and failed requests
  make firmware            firmware per host and per model, against the baseline
  make inventory           a row per host: identity, CPUs, GPUs, memory, drives, NICs, PSUs
  make telemetry           every sensor with its thresholds; hottest and highest power per host
  make capabilities        what each model can do: Redfish version, services, actions, update methods
  make diff                what changed since the previous collect
  make detail HOST=...     everything about one host
  make views               every view into <site>/reports/ as HTML and JSON

Rollout: firmware updates, a canary first, then waves, each after a gate
  make plan                pre-flight, read-only: per host, what it can update and how (COMPONENT for its waves)
  make dry-run             plan, canary and wave previews; no firmware changes
  make update              the real update, once make test passes: the lab only
  make report              the latest pipeline's report: the verdict, then every host (HOST=... for its steps)
  make watch               follow a running update from a second terminal
  make cancel              stop the running update before its next wave: the wave under way finishes, no host is
                           left mid-flash, and the locks go back (PIPELINE=pipeline-... for another than the newest)
  make fault SCENARIO=...  a bad update on the lab: silent-fail, unhealthy, rejected, bad-checksum, no-return, hybrid

COMPONENT is the firmware row to plan or update; dry-run and update take "Manager (BMC)" without it.
ARGS passes more options to the command, e.g. make health ARGS="--html health.html".
ROLLOUT_ACTOR names who plans, updates or cancels, in the plan, the run record and the report; without it, the account
and host make runs as.

First run: make setup lab-up, then make dry-run, make update, make report. make site-up SITE=prod reads the real
fleet into Grafana's Prod folder.
To roll the lab back, swap the commented line in control/catalog/lab/baseline.yaml and run make update again.
endef
export HELP

.DEFAULT_GOAL := help
.DELETE_ON_ERROR:
.PHONY: help setup test mutations control-up control-down state unlock site-up site-logs site-down lab-up lab-down \
        lab-reset lab-clean collect $(VIEWS) detail views plan dry-run update report watch cancel fault

help:
	@printf '%s\n' "$$HELP"

setup:
	python3 -m venv venv
	$(PY) -m pip install -r requirements.txt

# the gate: every change, and before any real update
test:
	$(DB) $(PY) tests/test_rollout.py

# the audit, before changing a safety rule: it finds each rule by its line, so it can't be the gate
mutations: control/netbox/.netbox.env control/state/.state.env control/observability/grafana/.grafana.env
	$(CONTROL) up --detach --wait state-postgres
	$(DB) $(PY) tests/mutations.py

# ---- Control plane ----
# Up, then NetBox's seed (the read-only token, and lab/inventory.yaml's servers when NetBox has no lab device yet), then
# the ingest into the store when the catalog is missing: download-fw-images.sh downloads, promote-fw-images.sh checks and
# uploads. Every package the catalog names must then be in the store: a store that just started answers before its
# volumes are registered, so each read is retried for 30 s
control-up: control/netbox/.netbox.env control/state/.state.env control/observability/grafana/.grafana.env
	python3 control/observability/grafana/prod_dashboards.py
	$(STATE_RO) $(CONTROL) up --detach --wait
	$(CONTROL) exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell --no-startup --no-imports \
	  --interface python < control/netbox/seed.py
	[ -f control/catalog/lab/images.yaml ] && [ -f control/catalog/lab/baseline.yaml ] && [ -f lab/bmc.mtd ] || \
	  { ./control/ingest/download-fw-images.sh && ./control/ingest/promote-fw-images.sh control/ingest/staging/*.tar; }
	@for f in $$(sed -n 's/.*{file: "\([^"]*\)".*/\1/p' control/catalog/lab/images.yaml); do \
	  ok=; for i in 1 2 3 4 5 6 7 8 9 10; do \
	    $(CONTROL) exec -T store wget -q --spider "http://127.0.0.1:8888/buckets/firmware/$$f" && { ok=1; break; }; sleep 3; \
	  done; \
	  [ -n "$$ok" ] || { echo "The store lost $$f, its volume deleted: make lab-clean lab-up ingests the newest builds again"; exit 1; }; \
	done

control-down:
	$(CONTROL) down

state:
	@$(CONTROL) exec -T state-postgres psql -U rollout -d rollout -c 'SELECT * FROM host_locks ORDER BY host' \
	  -c "SELECT DISTINCT ON (host) host, pipeline, stage, wave, step, state, at, left(detail, 80) AS detail \
	      FROM host_state ORDER BY host, at DESC"

# A person checked the host: its lock goes, whichever pipeline held it
unlock:
	$(if $(HOST),,$(error set HOST, e.g. make unlock HOST=127.1.0.1))
	@echo "DELETE FROM host_locks WHERE host = ANY(string_to_array(:'hosts', ' ')) RETURNING *" | \
	  $(CONTROL) exec -T state-postgres psql -U rollout -d rollout -v hosts="$(HOST)"

# ---- Site ----
# The site's exporter and agent, and on the lab the image cache; SITE_ENV sets each site's ports
site-up: $(if $(filter lab,$(SITE)),lab/cache/tls.crt)
	$(ENV) $(SITE_ENV) $(SITE_STACK) up --detach --build --wait

site-logs:
	$(ENV) $(SITE_ENV) $(SITE_STACK) logs --follow exporter agent

site-down:
	$(ENV) $(SITE_ENV) $(SITE_STACK) down

# ---- Lab ----
# The BMCs boot from the older build's bmc.mtd, which the control plane's ingest writes
lab-up: lab/qemu-system-arm control-up site-up
	$(LAB) up --detach --build --wait

lab-down:
	$(LAB) down

# The BMCs' flash volumes only: the store keeps its images, as a fleet reset never touches the approved store
lab-reset:
	$(LAB) down
	docker volume ls -q --filter name=redfish-lab_bmc | xargs -r docker volume rm

lab-clean:
	$(LAB) down --volumes
	rm -rf control/catalog/lab lab/bmc.mtd lab/spool control/ingest/staging

# OpenBMC's prebuilt QEMU, which emulates the GB200 NVL's BMC (an AST2600)
lab/qemu-system-arm:
	curl -sfL -o $@ https://jenkins.openbmc.org/job/latest-qemu-x86/lastSuccessfulBuild/artifact/qemu/build/qemu-system-arm
	chmod +x $@

# The image cache's self-signed certificate, for 127.0.0.1; rollout.py trusts it from lab/cache/tls.crt
lab/cache/tls.crt:
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=127.0.0.1 -addext subjectAltName=IP:127.0.0.1 \
	  -keyout lab/cache/tls.key -out $@ 2>/dev/null

# ---- Poller ----
collect:
	$(ENV) $(PY) poller.py collect $(ARGS)

$(VIEWS):
	$(ENV) $(PY) poller.py $@ $(ARGS)

detail:
	$(if $(HOST),,$(error set HOST, e.g. make detail HOST=127.1.0.1))
	$(ENV) $(PY) poller.py detail $(HOST) $(ARGS)

views:
	@mkdir -p $(SITE)/reports
	@for v in $(VIEWS); do \
	  $(ENV) COLUMNS=$(WIDTH) $(PY) poller.py $$v --html $(SITE)/reports/$$v.html --json $(SITE)/reports/$$v.json \
	    > /dev/null || exit 1; \
	  echo "$(SITE)/reports/$$v.html  $(SITE)/reports/$$v.json"; \
	done

# ---- Rollout ----
plan:
	$(ENV) $(PY) rollout.py plan $(if $(COMPONENT),"$(COMPONENT)") $(PLAN_ARGS) $(if $(HOST),--hosts $(HOST)) $(ARGS)

dry-run:
	$(ENV) YES= ./pipeline.sh "$(FIRMWARE)" $(PLAN_ARGS) $(ARGS)

update: test
	$(LAB_ONLY)
	$(ENV) YES=1 ./pipeline.sh "$(FIRMWARE)" $(PLAN_ARGS) $(ARGS)

report:
	$(ENV) $(PY) rollout.py report $(if $(HOST),--host $(HOST)) $(ARGS)

watch:
	watch -c -n 5 "SITE=$(SITE) COLUMNS=$(WIDTH) FORCE_COLOR=1 $(PY) rollout.py report"

# A person stops a pipeline: <pipeline>/cancel, with who and when, which rollout.py checks before each wave and during a
# soak. The wave under way finishes first: a host is never stopped mid-flash
cancel:
	@p=$(SITE)/runs/$${PIPELINE:-$$(ls -d $(SITE)/runs/pipeline-*/ 2>/dev/null | sort | tail -1 | xargs -r basename)}; \
	  [ -d "$$p" ] && [ "$$p" != "$(SITE)/runs/" ] || { echo "no pipeline in $(SITE)/runs/ to cancel"; exit 1; }; \
	  echo "$${ROLLOUT_ACTOR:-$${SUDO_USER:-$$(whoami)}@$$(hostname)} at $$(date -u +%FT%TZ)" > "$$p/cancel"; \
	  echo "Cancel requested: $$p stops before its next wave"

fault:
	$(LAB_ONLY)
	$(if $(SCENARIO),,$(error set SCENARIO: silent-fail, unhealthy, rejected, bad-checksum, no-return or hybrid))
	$(ENV) ./lab/scenario.sh "$(SCENARIO)" $(PLAN_ARGS) $(ARGS)

# ---- The control plane's secrets ----
# Grafana's admin account is yours to choose: make doesn't make it
control/observability/grafana/.grafana.env:
	@echo "First create $@ with GRAFANA_ADMIN_USERNAME=... and GRAFANA_ADMIN_PASSWORD=...: Grafana's admin" \
	  "account (README, Quick start)"; exit 1

# Its secrets, random and made once: never shown, read from the file. NETBOX_TOKEN is the tools' read-only API token
control/netbox/.netbox.env:
	@umask 077; python3 -c 'import secrets, string; \
	r = lambda n: "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n)); db = r(32); \
	print(f"SECRET_KEY={r(60)}\nAPI_TOKEN_PEPPER_1={r(60)}\nDB_PASSWORD={db}\nPOSTGRES_PASSWORD={db}\n" \
	      f"REDIS_PASSWORD={r(32)}\nSUPERUSER_PASSWORD={r(24)}\nNETBOX_TOKEN=nbt_{r(12)}.{r(40)}")' > $@
	@echo "Made $@: NetBox's secrets and the admin's password"

# Its passwords, random and made once: never shown, read from the file. The owner's, the rollout's (redfish_rollout_sa)
# and Grafana's
control/state/.state.env:
	@umask 077; python3 -c 'import secrets; \
	print("".join(f"{k}_PASSWORD={secrets.token_urlsafe(24)}\n" \
	              for k in ("POSTGRES", "REDFISH_ROLLOUT_SA", "GRAFANA")), end="")' > $@
	@echo "Made $@: the state database's passwords"
