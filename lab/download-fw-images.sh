#!/usr/bin/env bash
# The first half of the lab's firmware ingest: downloads OpenBMC's two newest successful GB200 NVL builds from its
# Jenkins, each a whole-flash update package, into lab/staging/, where promote-fw-images.sh checks them. Jenkins keeps
# only the last three builds, so no build can be pinned in the repository. make lab-up runs both when the catalog is
# missing. It writes only:
#   lab/staging/gb200nvl-<build>.static.mtd.all.tar
set -euo pipefail
cd "$(dirname "$0")"  # lab/
job='https://jenkins.openbmc.org/job/latest-master/label=docker-builder,target=gb200nvl-obmc'
files=artifact/openbmc/build/tmp/deploy/images/gb200nvl-obmc

# The successful builds, newest first
builds=$(curl -gsf "$job/api/json?tree=builds[number,result]" | python3 -c \
  'import json, sys; print(*[b["number"] for b in json.load(sys.stdin)["builds"] if b["result"] == "SUCCESS"])') ||
  { echo "Can't read the build list of $job" >&2; exit 1; }
read -r new old _ <<< "$builds"
[ -n "${old:-}" ] || { echo "Jenkins has fewer than two successful GB200 NVL builds now: try again later" >&2; exit 1; }

# Only these two: a package an earlier run left, refused or half-downloaded, is never promoted
rm -rf staging
mkdir staging
for build in "$old" "$new"; do
  # The package's name is date-stamped: take it from the build's file listing
  listing=$(curl -sf "$job/$build/$files/") || { echo "Can't read the files of build $build" >&2; exit 1; }
  name=$(grep -m1 -o 'obmc-phosphor-image-gb200nvl-obmc-[0-9]*\.static\.mtd\.all\.tar' <<< "$listing") ||
    { echo "No update package in build $build" >&2; exit 1; }
  name=${name%%$'\n'*}  # the listing names it several times on one line
  echo "Downloading build $build: $name (64 MiB)"
  curl -sfL -o "staging/gb200nvl-$build.static.mtd.all.tar" "$job/$build/$files/$name"
done
