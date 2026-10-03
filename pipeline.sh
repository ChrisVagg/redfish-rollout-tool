#!/usr/bin/env bash
# The rollout pipeline, one stage after the other: plan → canary → waves → report. A stage that fails stops the
# pipeline: rollout.py exits 1 when a gate fails (a canary failure, or more than --halt-at of the hosts failing). The
# report runs either way, over the records this pipeline wrote.
#
#   SITE=lab ./pipeline.sh COMPONENT [plan options]         every stage a dry run: the plan and what each host would get
#   SITE=lab YES=1 ./pipeline.sh COMPONENT [plan options]   really update, on hosts marked writable: true only
#
# SITE is the fleet's folder, prod (the default) or lab. On the lab: make dry-run and make update. The plan options
# (--allow-downgrade, --canary, --wave-pct...) go to the plan stage; the canary and waves stages take everything from
# the plan it saved, plus RUN_ARGS (--reset-timeout, --fault...). Each pipeline keeps its plan and HTML reports in
# <site>/runs/pipeline-<time>/.
set -uo pipefail
cd "$(dirname "$0")"
component=${1:?usage: [SITE=lab] [YES=1] ./pipeline.sh COMPONENT [plan options]}
shift
py=${PY:-venv/bin/python}
runs=${SITE:-prod}/runs  # where rollout.py records the site's runs
out=$runs/pipeline-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$out"
yes=$([ "${YES:-}" = 1 ] && echo --yes)  # only YES=1 writes: YES=0 or YES=no stays a dry run
read -ra run_args <<< "${RUN_ARGS:-}"

# Run one stage: a banner, the command, how it ended and how long it took; returns the command's exit status
stage() {
  local name=$1 start=$SECONDS status
  shift
  printf '\n━━━ stage %s · %s UTC ━━━\n' "$name" "$(date -u +%H:%M:%S)"
  "$@"
  status=$?
  printf '━━━ stage %s: %s after %ss ━━━\n' "$name" \
    "$([ "$status" -eq 0 ] && echo passed || echo "FAILED, exit $status")" $((SECONDS - start))
  return "$status"
}

status=0
stage plan "$py" rollout.py plan "$component" "$@" --save "$out/plan.json" --html "$out/plan.html" || status=$?
if [ "$status" -eq 0 ]; then
  stage canary "$py" rollout.py run --plan "$out/plan.json" --stage canary $yes "${run_args[@]}" \
    --html "$out/canary.html" &&
    stage waves "$py" rollout.py run --plan "$out/plan.json" --stage waves $yes "${run_args[@]}" \
      --html "$out/waves.html"
  status=$?
fi

# The report: from the plan and the records this pipeline's stages wrote (newer than the plan). A dry run has none;
# a plan that blocked every host has none either, and its report says so. report.json is for services, report.html
# has every host's steps too.
records=()
[ -f "$out/plan.json" ] &&
  mapfile -t records < <(find "$runs" -maxdepth 1 -name '*.jsonl' -newer "$out/plan.json" | sort)
if [ -f "$out/plan.json" ] && { [ "${#records[@]}" -gt 0 ] || [ "$status" -ne 0 ]; }; then
  [ "${#records[@]}" -gt 0 ] && printf '%s\n' "${records[@]}" > "$out/records.txt"
  stage report "$py" rollout.py report --plan "$out/plan.json" "${records[@]}" --save "$out/report.json"
  COLUMNS=200 "$py" rollout.py report --plan "$out/plan.json" "${records[@]}" --details --html "$out/report.html" \
    > /dev/null
fi
printf '\nPipeline %s. Plan and reports: %s/\n' \
  "$([ "$status" -eq 0 ] && echo passed || echo 'stopped: a stage FAILED')" "$out"
[ -z "$yes" ] && echo "Every stage was a dry run: nothing was changed. YES=1 (make update) to update."
exit "$status"
