#!/usr/bin/env bash
# The lab's firmware, from OpenBMC's Jenkins: its two newest GB200 NVL builds. Jenkins keeps only the last three builds,
# so a build can't be pinned in the repository. make lab-up runs this when one of the files below is missing:
#   lab/images/gb200nvl-<build>.static.mtd.all.tar  both builds' whole-flash update packages
#   lab/bmc.mtd                                     the older build's flash: what the lab BMCs boot
#   lab/images.yaml                                 the catalog: both packages with their sha256
#   lab/baseline.yaml                               the newer build approved, so the first rollout is an update
set -euo pipefail
cd "$(dirname "$0")"  # lab/: the catalog's files are relative to it
job='https://jenkins.openbmc.org/job/latest-master/label=docker-builder,target=gb200nvl-obmc'
files=artifact/openbmc/build/tmp/deploy/images/gb200nvl-obmc

# The successful builds, newest first
builds=$(curl -gsf "$job/api/json?tree=builds[number,result]" | python3 -c \
  'import json, sys; print(*[b["number"] for b in json.load(sys.stdin)["builds"] if b["result"] == "SUCCESS"])') ||
  { echo "Can't read the build list of $job" >&2; exit 1; }
read -r new old _ <<< "$builds"
[ -n "${old:-}" ] || { echo "Jenkins has fewer than two successful GB200 NVL builds now: try again later" >&2; exit 1; }

mkdir -p images
for build in "$old" "$new"; do
  file=images/gb200nvl-$build.static.mtd.all.tar
  # The package's name is date-stamped: take it from the build's file listing
  listing=$(curl -sf "$job/$build/$files/") || { echo "Can't read the files of build $build" >&2; exit 1; }
  name=$(grep -m1 -o 'obmc-phosphor-image-gb200nvl-obmc-[0-9]*\.static\.mtd\.all\.tar' <<< "$listing") ||
    { echo "No update package in build $build" >&2; exit 1; }
  name=${name%%$'\n'*}  # the listing names it several times on one line
  if [ ! -f "$file" ]; then
    echo "Downloading build $build: $name (64 MiB)"
    curl -sfL -o "$file.part" "$job/$build/$files/$name"
    mv "$file.part" "$file"
  fi
done

version() { tar -xOf "images/gb200nvl-$1.static.mtd.all.tar" MANIFEST | sed -n 's/^version=//p'; }
entry() {
  local file=images/gb200nvl-$1.static.mtd.all.tar
  echo "    \"$(version "$1")\": {file: \"$file\", sha256: \"$(sha256sum "$file" | cut -d' ' -f1)\"}"
}

tar -xOf "images/gb200nvl-$old.static.mtd.all.tar" image-bmc > bmc.mtd
{
  echo "# Written by lab/images.sh: the lab's firmware images, GB200 NVL whole-flash update packages of OpenBMC's"
  echo "# Jenkins builds $old and $new"
  echo '"- -":'
  echo '  "Manager (BMC)":'
  entry "$old"
  entry "$new"
} > images.yaml
cat > baseline.yaml <<EOF
# Written by lab/images.sh: the lab's approved firmware. The emulated GB200 NVL reports no manufacturer or model, so its
# model reads "- -". The lab boots build $old and this approves build $new, so make update updates it. To
# roll the lab back and forth, swap which line is commented.
"- -":
  "Manager (BMC)": "$(version "$new")"  # build $new
  # "Manager (BMC)": "$(version "$old")"  # build $old, the one the lab boots
EOF
echo "Lab firmware: boots build $old ($(version "$old")), baseline approves build $new ($(version "$new"))"
