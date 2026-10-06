# Redfish firmware tooling: `make` lists every target. Four parts:
#   poller.py       reads the resources each BMC's Redfish service exposes: health, firmware, inventory, telemetry
#   rollout.py      updates firmware: pre-flight, a canary, then waves, each after a gate (pipeline.sh runs the stages)
#   lab/            10 emulated OpenBMC BMCs, QEMU in Docker: the only BMCs the rollout updates
#   observability/  exporter-lab, exporter-prod, Pushgateway, Loki, Prometheus and Grafana, in Docker
# Every target runs on the lab unless SITE=prod: the real fleet in prod/, read-only.

SITE      ?= lab
PY        := venv/bin/python
VIEWS     := health firmware inventory telemetry capabilities diff
WIDTH     := 200
LAB       := docker compose -f lab/docker-compose.yaml
MONITOR   := docker compose -f observability/docker-compose.yaml
# the firmware dry-run and update work on
FIRMWARE   = $(or $(COMPONENT),Manager (BMC))

ifeq ($(SITE),lab)
  # OpenBMC's public default account; the rollout pushes its metrics to the Pushgateway and its events to Loki
  ENV       := SITE=lab LAB_USERNAME=root LAB_PASSWORD=0penBmc PUSHGATEWAY=http://127.0.0.1:9091 \
               LOKI=http://127.0.0.1:3100 IMAGE_CACHE=https://127.0.0.1:8443
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
  make test                the rollout's decisions on recorded Redfish data, in about a second, no BMC needed
  make mutations           break each safety rule of rollout.py, in a copy: each break must fail a test

Lab: 10 emulated OpenBMC BMCs, QEMU in Docker, on 127.0.0.1:2441-2450, and their HTTPS image cache on :8443
  make lab-up              start the image store, ingest OpenBMC's two newest builds into it (download, check
                           their signatures, upload), boot the BMCs (about 10 min)
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
  make views               every view into <site>/reports/ as HTML, JSON and CSV

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
        monitor-up monitor-logs monitor-down

help:
	@printf '%s\n' "$$HELP"

setup:
	python3 -m venv venv
	$(PY) -m pip install -r requirements.txt

# the gate: every change, and before any real update
test:
	$(PY) test_rollout.py

# the audit, before changing a safety rule: it finds each rule by its line, so it can't be the gate
mutations:
	$(PY) mutations.py

# ---- Lab ----
# The store first, then the ingest into it: download-fw-images.sh downloads, promote-fw-images.sh checks and uploads. Then
# every package the catalog names must be in the store, before the BMCs boot from the older build's bmc.mtd
lab-up: lab/qemu-system-arm lab/cache/tls.crt
	$(LAB) up --detach --wait store
	[ -f lab/images.yaml ] && [ -f lab/baseline.yaml ] && [ -f lab/bmc.mtd ] || \
	  { ./lab/download-fw-images.sh && ./lab/promote-fw-images.sh lab/staging/*.tar; }
	@for f in $$(sed -n 's/.*{file: "\([^"]*\)".*/\1/p' lab/images.yaml); do \
	  $(LAB) exec -T store wget -q --spider "http://127.0.0.1:8888/buckets/firmware/$$f" || \
	  { echo "The store lost $$f, its volume deleted: make lab-clean lab-up ingests the newest builds again"; exit 1; }; \
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
	$(if $(HOST),,$(error set HOST, e.g. make detail HOST=127.0.0.1:2441))
	$(ENV) $(PY) poller.py detail $(HOST) $(ARGS)

views:
	@mkdir -p $(SITE)/reports
	@for v in $(VIEWS); do \
	  $(ENV) COLUMNS=$(WIDTH) $(PY) poller.py $$v --html $(SITE)/reports/$$v.html --json $(SITE)/reports/$$v.json \
	    --csv $(SITE)/reports/$$v > /dev/null || exit 1; \
	  echo "$(SITE)/reports/$$v.html  $(SITE)/reports/$$v.json  $(SITE)/reports/$$v/"; \
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
monitor-up:
	@[ -f observability/grafana/.grafana.env ] || { echo "First create observability/grafana/.grafana.env with" \
	  "GRAFANA_ADMIN_USERNAME=... and GRAFANA_ADMIN_PASSWORD=...: Grafana's admin account (README, Quick start)"; exit 1; }
	python3 observability/grafana/prod_dashboards.py
	$(MONITOR) up --detach --build --wait

monitor-logs:
	$(MONITOR) logs --follow exporter-lab exporter-prod

monitor-down:
	$(MONITOR) down
