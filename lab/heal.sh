#!/usr/bin/env bash
# After investigate the error which produced in a manual test - QA.
# Restart the lab BMCs whose boot went wrong, until every one reports its Manager Service Enabled. An emulated BMC boots slowly,
# and now and then a service that writes the U-Boot environment runs before /dev/mtd/u-boot-env exists: systemd ends
# degraded, and bmcweb reports the Manager Quiesced, health Critical. A fresh boot fixes it. make lab-up runs this.

set -euo pipefail
cd "$(dirname "$0")"
lab="docker compose -f docker-compose.yaml"
# The BMCs: the other services have no Redfish. None at all means Compose failed, not a healthy lab
bmcs=$($lab ps --services | grep "^bmc") || { echo "No lab BMC is running: make lab-up" >&2; exit 1; }
# The Manager's State once the boot has settled: /redfish/v1 answers while systemd is still starting services, and a
# boot still going (Starting, or no answer yet) isn't a degraded one, so wait up to 5 min for it to end
settled() {
  local state
  for _ in $(seq 30); do
    state=$(curl -sk -m 20 -u root:0penBmc "https://127.0.0.1:$1/redfish/v1/Managers/bmc" |
      python3 -c 'import json, sys; print(json.load(sys.stdin)["Status"].get("State"))' 2>/dev/null || echo unknown)
    case $state in Starting | unknown) sleep 10 ;; *) break ;; esac
  done
  echo "$state"
}
# Round 4 only checks: every restart is verified
for round in 1 2 3 4; do
  degraded=()
  for service in $bmcs; do
    state=$(settled "$($lab port "$service" 443 | cut -d: -f2)")
    [ "$state" = Enabled ] || degraded+=("$service ($state)")
  done
  [ "${#degraded[@]}" -eq 0 ] && { echo "Every lab BMC reports its Manager Enabled"; exit 0; }
  [ "$round" -eq 4 ] && break
  echo "Degraded boot, restarting (round $round): ${degraded[*]}"
  degraded=("${degraded[@]%% *}")  # the service names alone
  $lab restart "${degraded[@]}" > /dev/null
  # Wait for the restarted ones to answer again; up --wait could recreate, and so reboot, every BMC
  for service in "${degraded[@]}"; do
    timeout 900 bash -c "until [ \"\$($lab ps --format '{{.Health}}' $service)\" = healthy ]; do sleep 10; done"
  done
done
echo "Still degraded after 3 restarts: ${degraded[*]}" >&2
exit 1
