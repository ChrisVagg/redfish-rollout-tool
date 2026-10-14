# Redfish firmware tooling: `make` lists every target. Four parts:
#   poller.py       reads the resources each BMC's Redfish service exposes: health, firmware, inventory, telemetry
#   rollout.py      updates firmware: pre-flight, a canary, then waves, each after a gate (pipeline.sh runs the stages)
#   lab/            10 emulated OpenBMC BMCs, QEMU in Docker: the only BMCs the rollout updates
#   observability/  exporter-lab, exporter-prod, Pushgateway, Loki, Prometheus and Grafana, in Docker
#   netbox/         NetBox, the lab inventory's source of truth, in Docker
#   state/          PostgreSQL, the rollout state: every event of every run, and a lock per host, in Docker
# Every target runs on the lab unless SITE=prod: the real fleet in prod/, read-only.

SITE      ?= lab
PY        := venv/bin/python
VIEWS     := health firmware inventory telemetry capabilities diff
WIDTH     := 200
LAB       := docker compose -f lab/docker-compose.yaml
MONITOR   := docker compose -f observability/docker-compose.yaml
NETBOX    := docker compose -f netbox/docker-compose.yaml
STATE     := docker compose -f state/docker-compose.yaml
# the tools' read-only NetBox token, from netbox/.netbox.env
TOKEN      = NETBOX_TOKEN=$$(sed -n 's/^NETBOX_TOKEN=//p' netbox/.netbox.env 2>/dev/null)
# the rollout state database, where an update locks its hosts and adds its events, as the account redfish_rollout_sa
DB         = STATE_DB=postgresql://redfish_rollout_sa:$$(sed -n 's/^REDFISH_ROLLOUT_SA_PASSWORD=//p' state/.state.env \
             2>/dev/null)@127.0.0.1:5432/rollout
# pgAdmin's login, and the read-only account it reads the database with
PGADMIN    = PGADMIN_DEFAULT_PASSWORD=$$(sed -n 's/^PGADMIN_PASSWORD=//p' state/.state.env 2>/dev/null) \
             VIEWER_PASSWORD=$$(sed -n 's/^VIEWER_PASSWORD=//p' state/.state.env 2>/dev/null)
# Grafana's read-only account on it
STATE_RO   = STATE_GRAFANA_PASSWORD=$$(sed -n 's/^GRAFANA_PASSWORD=//p' state/.state.env 2>/dev/null)
# the firmware dry-run and update work on
FIRMWARE   = $(or $(COMPONENT),Manager (BMC))

ifeq ($(SITE),lab)
  # OpenBMC's public default account; the rollout pushes its metrics to the Pushgateway and its events to Loki
  ENV       := SITE=lab LAB_USERNAME=root LAB_PASSWORD=0penBmc PUSHGATEWAY=http://127.0.0.1:9091 \
               LOKI=http://127.0.0.1:3100 IMAGE_CACHE=https://127.0.0.1:8443 $(TOKEN) $(DB)
  # pre-flight can't put OpenBMC versions in order, so the lab allows either direction
  PLAN_ARGS := --allow-downgrade
else ifeq ($(SITE),prod)
  # the BMC accounts, from prod/.env
  ENV       := set -a; [ ! -f prod/.env ] || . prod/.env; set +a; SITE=prod
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

NetBox: the lab's hosts, racks and projects; the tools read them from it
  make netbox-up           start it on http://127.0.0.1:8000 (admin's password: netbox/.netbox.env) and load
                           lab/inventory.yaml's servers into it, once
  make netbox-down         stop it; its data stays in volumes

State: PostgreSQL, every event of every run (15 days) and a lock per host: an update holds its hosts' locks
  make state-up            start it on 127.0.0.1:5432, and pgAdmin on http://127.0.0.1:5050 to look at its schema
                           and tables (login admin@example.com, PGADMIN_PASSWORD); lab-up starts them too. The
                           passwords: state/.state.env
  make state               every host's lock, then each host's latest state
  make unlock HOST=...     give back a host's lock once a person checked it; several: HOST="a b"
  make state-down          stop it; its data stays in a volume

Lab: 10 emulated OpenBMC BMCs, QEMU in Docker, on 127.1.0.1-10, and their HTTPS image cache on :8443
  make lab-up              start NetBox, the state database and the image store, ingest OpenBMC's two newest
                           builds into it (download, check their signatures, upload), boot the BMCs (about 10 min)
  make lab-down            stop them; each keeps its flash
  make lab-reset           stop them and wipe their flash: they boot the older build again; the store keeps its images
  make lab-clean           delete everything the lab made, store included: lab-up then starts from the newest builds

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
  make fault SCENARIO=...  a bad update on the lab: silent-fail, unhealthy, rejected, bad-checksum, no-return, hybrid

Observability: exporter-lab, exporter-prod, Pushgateway, Loki, Prometheus and Grafana, in Docker
  make monitor-up          start them; Grafana on http://127.0.0.1:3000 (account: observability/grafana/.grafana.env)
  make monitor-logs        follow the exporters' logs
  make monitor-down        stop them; their data stays in volumes

COMPONENT is the firmware row to plan or update; dry-run and update take "Manager (BMC)" without it.
ARGS passes more options to the command, e.g. make health ARGS="--html health.html".

First run: make setup lab-up monitor-up, then make dry-run, make update, make report.
To roll the lab back, swap the commented line in lab/baseline.yaml and run make update again.
endef
export HELP

.DEFAULT_GOAL := help
.DELETE_ON_ERROR:
.PHONY: help setup test mutations lab-up lab-down lab-reset lab-clean collect $(VIEWS) detail views plan dry-run update report watch fault \
        monitor-up monitor-logs monitor-down netbox-up netbox-down state-up state state-down unlock

help:
	@printf '%s\n' "$$HELP"

setup:
	python3 -m venv venv
	$(PY) -m pip install -r requirements.txt

# the gate: every change, and before any real update
test:
	$(DB) $(PY) test_rollout.py

# the audit, before changing a safety rule: it finds each rule by its line, so it can't be the gate
mutations: state-up
	$(DB) $(PY) mutations.py

# ---- Lab ----
# The store first, then the ingest into it: download-fw-images.sh downloads, promote-fw-images.sh checks and uploads. Then
# every package the catalog names must be in the store, before the BMCs boot from the older build's bmc.mtd. A store
# that just started answers before its volumes are registered, so each read is retried for 30 s
lab-up: lab/qemu-system-arm lab/cache/tls.crt netbox-up state-up
	$(LAB) up --detach --wait store
	[ -f lab/images.yaml ] && [ -f lab/baseline.yaml ] && [ -f lab/bmc.mtd ] || \
	  { ./lab/download-fw-images.sh && ./lab/promote-fw-images.sh lab/staging/*.tar; }
	@for f in $$(sed -n 's/.*{file: "\([^"]*\)".*/\1/p' lab/images.yaml); do \
	  ok=; for i in 1 2 3 4 5 6 7 8 9 10; do \
	    $(LAB) exec -T store wget -q --spider "http://127.0.0.1:8888/buckets/firmware/$$f" && { ok=1; break; }; sleep 3; \
	  done; \
	  [ -n "$$ok" ] || { echo "The store lost $$f, its volume deleted: make lab-clean lab-up ingests the newest builds again"; exit 1; }; \
	done
	$(LAB) up --detach --build --wait

lab-down:
	$(LAB) down

# The BMCs' flash volumes only: the store keeps its images, as a fleet reset never touches the approved store
lab-reset:
	$(LAB) down
	docker volume ls -q --filter name=redfish-lab_bmc | xargs -r docker volume rm

lab-clean:
	$(LAB) down --volumes
	rm -rf lab/images.yaml lab/baseline.yaml lab/bmc.mtd lab/spool lab/staging

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

fault:
	$(LAB_ONLY)
	$(if $(SCENARIO),,$(error set SCENARIO: silent-fail, unhealthy, rejected, bad-checksum, no-return or hybrid))
	$(ENV) ./lab/scenario.sh "$(SCENARIO)" $(PLAN_ARGS) $(ARGS)

# ---- Observability ----
monitor-up: state/.state.env
	@[ -f observability/grafana/.grafana.env ] || { echo "First create observability/grafana/.grafana.env with" \
	  "GRAFANA_ADMIN_USERNAME=... and GRAFANA_ADMIN_PASSWORD=...: Grafana's admin account (README, Quick start)"; exit 1; }
	python3 observability/grafana/prod_dashboards.py
	$(TOKEN) $(STATE_RO) $(MONITOR) up --detach --build --wait

monitor-logs:
	$(MONITOR) logs --follow exporter-lab exporter-prod

monitor-down:
	$(MONITOR) down

# ---- NetBox ----
# Its secrets, random and made once: never shown, read from the file. NETBOX_TOKEN is the tools' read-only API token
netbox/.netbox.env:
	@umask 077; python3 -c 'import secrets, string; \
	r = lambda n: "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n)); db = r(32); \
	print(f"SECRET_KEY={r(60)}\nAPI_TOKEN_PEPPER_1={r(60)}\nDB_PASSWORD={db}\nPOSTGRES_PASSWORD={db}\n" \
	      f"REDIS_PASSWORD={r(32)}\nSUPERUSER_PASSWORD={r(24)}\nNETBOX_TOKEN=nbt_{r(12)}.{r(40)}")' > $@
	@echo "Made $@: NetBox's secrets and the admin's password"

# Up, then the seed: the read-only token, and lab/inventory.yaml's servers when NetBox has no lab device yet
netbox-up: netbox/.netbox.env
	$(NETBOX) up --detach --wait
	$(NETBOX) exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell --no-startup --no-imports \
	  --interface python < netbox/seed.py

netbox-down:
	$(NETBOX) down

# ---- State ----
# Its passwords, random and made once: never shown, read from the file. The owner's, the rollout's (redfish_rollout_sa), Grafana's,
# pgAdmin's account on the database (viewer) and pgAdmin's login
state/.state.env:
	@umask 077; python3 -c 'import secrets; \
	print("".join(f"{k}_PASSWORD={secrets.token_urlsafe(24)}\n" \
	              for k in ("POSTGRES", "REDFISH_ROLLOUT_SA", "GRAFANA", "VIEWER", "PGADMIN")), end="")' > $@
	@echo "Made $@: the state database's passwords"

state-up: state/.state.env
	$(PGADMIN) $(STATE) up --detach --wait

state:
	@$(STATE) exec -T postgres psql -U rollout -d rollout -c 'SELECT * FROM host_locks ORDER BY host' \
	  -c "SELECT DISTINCT ON (host) host, pipeline, stage, wave, step, state, at, left(detail, 80) AS detail \
	      FROM host_state ORDER BY host, at DESC"

# A person checked the host: its lock goes, whichever pipeline held it
unlock:
	$(if $(HOST),,$(error set HOST, e.g. make unlock HOST=127.1.0.1))
	@echo "DELETE FROM host_locks WHERE host = ANY(string_to_array(:'hosts', ' ')) RETURNING *" | \
	  $(STATE) exec -T postgres psql -U rollout -d rollout -v hosts="$(HOST)"

state-down:
	$(STATE) down
