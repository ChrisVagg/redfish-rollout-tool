#!/usr/bin/env bash
# The third of the lab's three ingest steps, after verify-fw-package.sh: promotes the packages the sandbox verified into
# the store. It reads the sandbox's list, a line per package:
#   <package file name> <sha256> <version>
# and takes nothing in it on trust: each line must be one the sandbox writes, for a package download-fw-package.sh
# downloaded, and each sha256 must still be its file's, so a package changed since the sandbox checked it is refused.
# Every line is checked before the first upload. Each package is uploaded into the store's bucket firmware, as
# images/<its name>, and its download deleted: the store holds the only copy. Then it writes:
#   control/catalog/lab/images.yaml    the catalog: each package's path in the store and its sha256
#   control/catalog/lab/baseline.yaml  the newest build approved, so the first rollout is an update
#   lab/bmc.mtd                        the oldest build's image-bmc, its whole flash: the lab BMCs' initial firmware
#   ./control/ingest/promote-store-fw-package.sh control/ingest/staging/verified
set -euo pipefail
ingest=$(dirname "$0")
root=$ingest/../..
catalog=$root/control/catalog/lab
store() { docker compose -f "$root/control/docker-compose.yaml" exec -T store "$@"; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
[ $# -eq 1 ] && [ -s "$1" ] ||
  { echo "Usage: $0 VERIFIED, the list verify-fw-package.sh printed: make control-up runs the three steps" >&2; exit 1; }
staging=$(dirname "$1")

lines=()
while read -r name sha256 version; do  # oldest build first
  [[ $name =~ ^gb200nvl-[0-9]+\.static\.mtd\.all\.tar$ && $sha256 =~ ^[0-9a-f]{64}$ &&
     $version =~ ^[A-Za-z0-9._+-]+$ ]] || { echo "$1: not a line the sandbox writes: $name $sha256 $version" >&2; exit 1; }
  [ -f "$staging/$name" ] || { echo "$name: not in ${staging##*/}/" >&2; exit 1; }
  [ "$(sha256sum < "$staging/$name" | cut -d' ' -f1)" = "$sha256" ] ||
    { echo "$name: changed since the sandbox verified it" >&2; exit 1; }
  lines+=("$name $sha256 $version")
done < <(sort -V "$1")

builds=() versions=() entries=()
for line in "${lines[@]}"; do
  read -r name sha256 version <<< "$line"
  package=$staging/$name
  # Write to SeaweedFS store
  store sh -c "cat > /tmp/$name && weed filer.copy /tmp/$name http://127.0.0.1:8888/buckets/firmware/images/ \
    > /dev/null && rm /tmp/$name" < "$package"
  echo "Promoted into the store: images/$name"
  build=${name#gb200nvl-} build=${build%%.*}
  builds+=("$build") versions+=("$version")
  entries+=("    \"$version\": {file: \"images/$name\", sha256: \"$sha256\"}")
  # the oldest's image, to stdout only: the sandbox found nothing but regular files in it, and its sha256 is checked
  [ -f "$work/bmc.mtd" ] || tar -xOf "$package" image-bmc > "$work/bmc.mtd"
  rm "$package"
done
rm "$1"

old=${builds[0]} new=${builds[-1]}
mkdir -p "$catalog"
cp "$work/bmc.mtd" "$root/lab/bmc.mtd"
{
  echo "# Written by control/ingest/promote-store-fw-package.sh: the lab's firmware images, GB200 NVL whole-flash update"
  echo "# packages of OpenBMC's Jenkins builds ${builds[*]}, each at its path in the store"
  echo '"- -":'
  echo '  "Manager (BMC)":'
  printf '%s\n' "${entries[@]}"
} > "$catalog/images.yaml"
cat > "$catalog/baseline.yaml" <<EOF
# Written by control/ingest/promote-store-fw-package.sh: the lab's approved firmware. The emulated GB200 NVL reports no
# manufacturer or model, so its model reads "- -". The lab boots build $old and this approves build $new, so make update
# updates it. To roll the lab back and forth, swap which line is commented.
"- -":
  "Manager (BMC)": "${versions[-1]}"  # build $new
  # "Manager (BMC)": "${versions[0]}"  # build $old, the one the lab boots
EOF
echo "Lab firmware: boots build $old (${versions[0]}), baseline approves build $new (${versions[-1]})"
