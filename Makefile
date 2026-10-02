# Redfish firmware tooling for two sites, each a folder with its inventory, baseline, images and outputs:
#   prod/  the real fleet: read-only, the rollout never updates it
#   lab/   10 emulated OpenBMC BMCs (QEMU in Docker), where the rollout really updates firmware
# The tools read their site from SITE (prod by default). `make` lists each site's targets and the variables they take.
PY     := venv/bin/python
POLL   := $(PY) poller.py
VIEWS  := health firmware inventory telemetry capabilities diff
WIDTH  := 200
DOCKER := docker compose -f lab/docker-compose.yaml
# Monitoring for both sites: an exporter each, Prometheus, Grafana
MONITOR := docker compose -f observability/docker-compose.yaml
# prod: the BMC credentials, from prod/.env
PROD   := set -a; [ ! -f prod/.env ] || . prod/.env; set +a; SITE=prod
# lab: OpenBMC's public default account
LAB    := SITE=lab LAB_USERNAME=root LAB_PASSWORD=0penBmc PUSHGATEWAY=http://127.0.0.1:9091
# Pre-flight can't put OpenBMC versions in order, so the lab allows either direction. The waves come from each site's
# rollout.yaml: lab/rollout.yaml plans a canary of 1, then waves of 3 and 6.
LAB_PLAN  := --allow-downgrade
LAB_COMPONENT = $(or $(COMPONENT),Manager (BMC))

.DEFAULT_GOAL := help
.DELETE_ON_ERROR:
.PHONY: help setup prod-collect prod-detail prod-report prod-plan lab-up lab-collect lab-detail lab-report lab-plan \
        lab-pipeline-plan lab-pipeline-update lab-pipeline-watch lab-pipeline-report lab-pipeline-fault lab-down \
        lab-reset monitor-up monitor-down monitor-logs

help: ## list the targets and the variables they take
	@awk -F':.*## ' '/^##@ / {printf "\n%s\n", substr($$0, 5)} \
	  /^[a-z%-]+:.*## / {t = $$1; sub("%", "<view>", t); printf "  make %-20s %s\n", t, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo 'Views, <view> above: read from the snapshots of the last collect'
	@echo '  health          health counts per host, then everything not OK, jobs to check and failed requests'
	@echo '  firmware        baseline compliance per host, then firmware per model against the baseline'
	@echo '  inventory       one row per host: identity, CPUs, GPUs, memory, drives, NICs, PSUs'
	@echo '  telemetry       every sensor of every host with its limits; hottest and highest power per host'
	@echo '  capabilities    what each hardware model can do: Redfish version, services, actions, update methods'
	@echo '  diff            what changed since the previous collect'
	@echo
	@echo 'Variables, set after the target: make prod-detail HOST=10.0.0.5'
	@echo '  HOST=<bmc address>        one host only'
	@echo '  COMPONENT="<firmware>"    the firmware row to plan or update; the lab updates "Manager (BMC)" by default'
	@echo '  SCENARIO=<name>           silent-fail, unhealthy, rejected, bad-checksum, no-return or hybrid'
	@echo '  ARGS="…"                  more options for the command, e.g. make prod-health ARGS="--html health.html"'
	@echo
	@echo 'The lab, in order: make setup lab-up monitor-up, then lab-pipeline-plan, lab-pipeline-update, lab-pipeline-report.'
	@echo 'lab/baseline.yaml approves the version the lab updates to: swap its commented line to roll back and forth.'

setup: ## create venv/ and install requirements.txt
	python3 -m venv venv
	$(PY) -m pip install -r requirements.txt

##@ Prod: the real fleet in prod/ (inventory.yaml, baseline.yaml, BMC credentials in .env), read-only
prod-collect: ## crawl every BMC into prod/snapshots/, which the views read
	$(PROD) $(POLL) collect $(ARGS)

prod-%:       ## a view of the last prod-collect, e.g. make prod-health (views below)
	SITE=prod $(POLL) $* $(ARGS)

prod-detail:  ## everything about one host · HOST
	$(if $(HOST),,$(error set HOST, e.g. make prod-detail HOST=<bmc address>))
	SITE=prod $(POLL) detail $(HOST) $(ARGS)

prod-report:  ## every view into prod/reports/ as HTML, JSON and CSV

prod-plan:    ## pre-flight: one report per host, what it can update and how · HOST · COMPONENT for its waves
	$(PROD) $(PY) rollout.py plan $(if $(COMPONENT),"$(COMPONENT)") $(if $(HOST),--hosts $(HOST)) $(ARGS)

##@ Lab: 10 emulated OpenBMC GB200 NVL BMCs in lab/ (QEMU in Docker) on 127.0.0.1:2441-2450, the only BMCs updated
lab-up: lab/qemu-system-arm lab/bmc.mtd lab/images.yaml lab/baseline.yaml
lab-up:       ## download QEMU and the two newest OpenBMC builds, then start the BMCs. Estimate around ~ 5m
	$(DOCKER) up --detach --build --wait
	./lab/heal.sh

lab-collect:  ## crawl the BMCs into lab/snapshots/, which the views read
	$(LAB) $(POLL) collect $(ARGS)

lab-%:        ## a view of the last lab-collect, e.g. make lab-health (views below)
	SITE=lab $(POLL) $* $(ARGS)

lab-detail:   ## everything about one BMC · HOST
	$(if $(HOST),,$(error set HOST, e.g. make lab-detail HOST=127.0.0.1:2441))
	SITE=lab $(POLL) detail $(HOST) $(ARGS)

lab-report:   ## every view into lab/reports/ as HTML, JSON and CSV

lab-plan:     ## pre-flight, read-only: one report per BMC, what it can update and how · HOST
	$(LAB) $(PY) rollout.py plan $(LAB_PLAN) $(if $(HOST),--hosts $(HOST)) $(ARGS)

lab-pipeline-plan:   ## dry run of the rollout: plan, canary, waves; nothing is written · COMPONENT
	$(LAB) YES= ./pipeline.sh "$(LAB_COMPONENT)" $(LAB_PLAN) $(ARGS)

lab-pipeline-update: ## the rollout: the canary (1 BMC), then waves of 3 and 6, each after a gate · COMPONENT
	$(LAB) YES=1 ./pipeline.sh "$(LAB_COMPONENT)" $(LAB_PLAN) $(ARGS)

lab-pipeline-watch:  ## follow a running pipeline from a second terminal, redrawn every 5s.
	watch -c -n 5 "SITE=lab COLUMNS=$(WIDTH) FORCE_COLOR=1 $(PY) rollout.py report"

lab-pipeline-report: ## the latest pipeline's report: the verdict, what needs a person, every host by wave · HOST
	SITE=lab $(PY) rollout.py report $(if $(HOST),--host $(HOST)) $(ARGS)

lab-pipeline-fault:  ## a bad update on the canary: the gate stops the pipeline, the rollback runs · SCENARIO
	$(LAB) ./lab/scenario.sh "$(SCENARIO)" $(LAB_PLAN) $(ARGS)

lab-down:     ## stop the BMCs; each keeps its flash volume
	$(DOCKER) down

lab-reset:    ## stop the BMCs and delete flash volumes: they boot the older build again
	$(DOCKER) down --volumes

##@ Monitoring: Prometheus, Grafana and an exporter per site in observability/, on 127.0.0.1 only
monitor-up:   ## start them: Grafana on http://127.0.0.1:3000, a Lab and a Prod folder: Fleet manager, Host, Pipeline
	$(MONITOR) up --detach --build --wait

monitor-logs: ## follow the exporters' logs: a host left out of a read, a refused login
	$(MONITOR) logs --follow exporter-lab exporter-prod

monitor-down: ## stop them; Prometheus and Grafana keep their data in volumes
	$(MONITOR) down

# Every view of a site into <site>/reports/
prod-report lab-report: %-report:
	@mkdir -p $*/reports
	@for v in $(VIEWS); do \
	  SITE=$* COLUMNS=$(WIDTH) $(POLL) $$v --html $*/reports/$$v.html --json $*/reports/$$v.json --csv $*/reports/$$v \
	    > /dev/null || exit 1; \
	  echo "$*/reports/$$v.html  $*/reports/$$v.json  $*/reports/$$v/"; \
	done

# OpenBMC's prebuilt QEMU, which emulates the GB200 NVL's BMC (an AST2600)
lab/qemu-system-arm:
	curl -sfL -o $@ https://jenkins.openbmc.org/job/latest-qemu-x86/lastSuccessfulBuild/artifact/qemu/build/qemu-system-arm
	chmod +x $@

# Firmware GB200 NVL - nvidia firwmare on top of openBMC -> bmcweb - redfish service
lab/bmc.mtd lab/images.yaml lab/baseline.yaml &:
	./lab/images.sh
