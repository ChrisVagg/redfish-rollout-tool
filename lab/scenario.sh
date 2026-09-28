#!/usr/bin/env bash
# Bad firmware updates on the lab, to see what the rollout does with them: the pipeline for real, to the build the lab
# doesn't run, with faults injected where real ones would show (rollout.py run --fault [HOST=]KIND). The script then
# checks how each host ended. lab/rollout.yaml plans a canary (127.0.0.1:2441), then waves of 3, then 6.
#
#   silent-fail   The BMC takes the image and reports Completed, but the firmware doesn't change: it got the running
#                 version's package as the target's. Post-check fails, the rollback reinstalls the version before.
#   unhealthy     The new firmware flashes and boots, then fails the post-check: a fault injected where a health
#                 regression would show. The rollback reflashes the version before.
#   rejected      The BMC refuses the image: the payload is cut to 8 MiB on its way. The update fails before any reset,
#                 so the old firmware still runs and there's nothing to roll back.
#   bad-checksum  The image file doesn't match the catalog's sha256: pre-flight blocks every host, none is touched.
#   no-return     The BMC isn't back within the reset timeout (45 s here; a boot takes about 3 min). The rollback
#                 reinstalls the version before, but its reset times out too, so nothing is verified:
#                 needs_attention, left for a person. The BMC comes back later, unverified.
#   hybrid        Good and bad updates in one rollout: the canary passes its soak, then wave 1 has a good host and two
#                 bad ones (2443 unhealthy, 2444 rejected). Wave 1 is strict, so any failure halts: wave 2 (6 BMCs)
#                 never starts, and the fleet is left on two builds for a person to look at.
#
#   make lab-pipeline-fault SCENARIO=<name>
set -euo pipefail
cd "$(dirname "$0")/.."
export SITE=lab
# How each host should end: canary is the plan's canary; untouched, never reached by the run
declare -A expect=(
  [silent-fail]="canary=rolled_back" [unhealthy]="canary=rolled_back" [rejected]="canary=failed"
  [bad-checksum]="canary=blocked" [no-return]="canary=needs_attention"
  [hybrid]="127.0.0.1:2441=updated 127.0.0.1:2442=updated 127.0.0.1:2443=rolled_back 127.0.0.1:2444=failed
            127.0.0.1:2445=untouched 127.0.0.1:2446=untouched 127.0.0.1:2447=untouched 127.0.0.1:2448=untouched
            127.0.0.1:2449=untouched 127.0.0.1:2450=untouched")
declare -A faults=(
  [silent-fail]="--fault silent-fail" [unhealthy]="--fault unhealthy" [rejected]="--fault rejected"
  [no-return]="--fault no-return" [bad-checksum]=""
  [hybrid]="--fault 127.0.0.1:2443=unhealthy --fault 127.0.0.1:2444=rejected")
scenario=${1:-none}
[ -n "${expect[$scenario]:-}" ] || { sed -n '2,/^#   make/p' "$0" | cut -c3-; exit 2; }  # the scenarios above
shift
: "${LAB_USERNAME:?the lab login: run it through make lab-pipeline-fault}" "${LAB_PASSWORD:?}"
bmc=127.0.0.1:2441  # every lab BMC runs the same build between scenarios: this one tells which

# What the lab runs now, and the catalog's other version: the target
get() { curl -sfk -m 30 -u "$LAB_USERNAME:$LAB_PASSWORD" "https://$bmc$1"; }
manager=$(get /redfish/v1/Managers |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["Members"][0]["@odata.id"])')
running=$(get "$manager" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("FirmwareVersion") or "")')
versions=$(sed -n 's/^    "\([^"]*\)": {file: .*/\1/p' lab/images.yaml)
grep -qxF "$running" <<< "$versions" || { echo "$bmc runs '$running', which lab/images.yaml doesn't list" >&2; exit 1; }
target=$(grep -vxF "$running" <<< "$versions")

# The target approved, and for bad-checksum a catalog whose sha256 for it is wrong
mkdir -p lab/scenario
catalog=lab/images.yaml
cat > lab/scenario/baseline.yaml <<EOF
# Written by lab/scenario.sh for $scenario: the target of the bad update
"- -":
  "Manager (BMC)": "$target"
EOF
if [ "$scenario" = bad-checksum ]; then
  catalog=lab/scenario/images.yaml
  sed "/\"$target\":/s/sha256: \"[0-9a-f]*\"/sha256: \"$(printf '0%.0s' {1..64})\"/" lab/images.yaml > "$catalog"
fi

printf '\n━━━ scenario %s: %s → %s%s ━━━\n' "$scenario" "$running" "$target" \
  "${faults[$scenario]:+ · ${faults[$scenario]}}"
status=0
YES=1 RUN_ARGS="${faults[$scenario]}" ./pipeline.sh "Manager (BMC)" --baseline lab/scenario/baseline.yaml \
  --images "$catalog" "$@" || status=$?

# How each host ended: its done event; blocked or skipped at pre-flight; untouched when the run never reached it
out=$(ls -d lab/runs/pipeline-* | tail -1)
echo "$scenario" > "$out/scenario.txt"
canary=$(python3 -c 'import json, sys; print(*json.load(open(sys.argv[1]))["waves"].get("canary", ["-"])[:1])' \
  "$out/plan.json")
ended() {
  local events state
  [ -f "$out/records.txt" ] || { echo blocked; return; }
  events=$(xargs grep -hF "\"host\": \"$1\"" < "$out/records.txt" || true)
  state() { grep -F "\"step\": \"$1\"" <<< "$events" | sed -n 's/.*"state": "\([^"]*\)".*/\1/p' | tail -1; }
  if [ -n "$(state done)" ]; then state done
  elif [ -n "$(state pre-flight)" ]; then echo "$(state pre-flight)" | sed 's/^block$/blocked/; s/^skip$/skipped/'
  else echo untouched; fi
}
ok=$([ "$status" -ne 0 ] && echo yes || echo no)
printf '\n'
for pair in ${expect[$scenario]}; do
  host=${pair%=*} want=${pair#*=}
  [ "$host" = canary ] && host=${canary/#-/no canary, the plan}
  got=$(ended "$host")
  [ "$got" = "$want" ] && mark=✓ || { mark=✗; ok=no; }
  printf '%s %s: %s (expected %s)\n' "$mark" "$host" "$got" "$want"
done
# The BMCs that refused an image
for pair in ${faults[$scenario]//--fault /}; do
  case $pair in
    rejected) port=${canary##*:} ;;
    *=rejected) port=${pair%=*}; port=${port##*:} ;;
    *) continue ;;
  esac
  echo "bmc${port: -1} keeps the aborted task until it restarts, and pre-flight blocks it until then:" \
    "docker compose -f lab/compose.yaml restart bmc${port: -1}"
done
if [ "$ok" = yes ]; then
  printf '✓ scenario %s: every host ended as expected and the pipeline stopped at its gate (exit %s)\n' \
    "$scenario" "$status"
else
  printf '✗ scenario %s: not as expected (pipeline exit %s)\n' "$scenario" "$status"
  exit 1
fi
