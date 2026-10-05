#!/usr/bin/env bash
# The second half of the lab's firmware ingest, after download-fw-images.sh: promotes OpenBMC update packages into the
# store. First the gate: the RSA-SHA256 signature of every signed file in a package (its MANIFEST, its key and each
# image) must verify with the key pinned here, lab/openbmc-dev.pub, never with the key the package carries, which anyone
# could replace together with the image. A package that fails stops it, before the store. The pinned key is OpenBMC's
# development key, whose private half is in OpenBMC's public source tree: it proves a package is unchanged since its
# build, not who built it. In production the pinned key is the vendor's. A package that passes is uploaded into the
# store's bucket firmware, as images/<its name>, and its download deleted: the store holds the only copy. Then, from the
# verified packages, it writes:
#   lab/images.yaml    the catalog: each package's path in the store and its sha256
#   lab/baseline.yaml  the newest build approved, so the first rollout is an update
#   lab/bmc.mtd        the oldest build's image-bmc, its whole flash: the BMCs' initial firmware when the lab is created
#   ./lab/promote-fw-images.sh lab/staging/*.tar
set -euo pipefail
lab=$(dirname "$0")
key=$lab/openbmc-dev.pub
store() { docker compose -f "$lab/docker-compose.yaml" exec -T store "$@"; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
[ $# -gt 0 ] || { echo "Usage: $0 PACKAGE.tar...: make lab-up downloads them first" >&2; exit 1; }

builds=() versions=() entries=()
while read -r package; do  # oldest build first
  name=${package##*/}
  dir=$(mktemp -d -p "$work")  # each package in a folder of its own
  tar -xf "$package" -C "$dir"
  signed=0
  for sig in "$dir"/*.sig; do
    file=${sig%.sig}
    [ -f "$file" ] || continue  # image-full.sig signs the files together, not one file
    openssl dgst -sha256 -verify "$key" -signature "$sig" "$file" > /dev/null 2>&1 ||
      { echo "$package: ${file##*/}: its signature doesn't verify with ${key##*/}" >&2; exit 1; }
    signed=$((signed + 1))
  done
  [ -f "$dir/MANIFEST.sig" ] && [ "$signed" -ge 2 ] || { echo "$package: not signed" >&2; exit 1; }
  echo "$name: $signed signatures verified with ${key##*/}"

  store sh -c "cat > /tmp/$name && weed filer.copy /tmp/$name http://127.0.0.1:8888/buckets/firmware/images/ \
    > /dev/null && rm /tmp/$name" < "$package"
  echo "Promoted into the store: images/$name"
  build=${name#gb200nvl-} build=${build%%.*}
  version=$(sed -n 's/^version=//p' "$dir/MANIFEST")
  builds+=("$build") versions+=("$version")
  entries+=("    \"$version\": {file: \"images/$name\", sha256: \"$(sha256sum < "$package" | cut -d' ' -f1)\"}")
  [ -f "$work/bmc.mtd" ] || cp "$dir/image-bmc" "$work/bmc.mtd"  # the oldest's
  rm "$package"
done < <(printf '%s\n' "$@" | sort -V)

old=${builds[0]} new=${builds[-1]}
cp "$work/bmc.mtd" "$lab/bmc.mtd"
{
  echo "# Written by lab/promote-fw-images.sh: the lab's firmware images, GB200 NVL whole-flash update packages of"
  echo "# OpenBMC's Jenkins builds ${builds[*]}, each at its path in the store"
  echo '"- -":'
  echo '  "Manager (BMC)":'
  printf '%s\n' "${entries[@]}"
} > "$lab/images.yaml"
cat > "$lab/baseline.yaml" <<EOF
# Written by lab/promote-fw-images.sh: the lab's approved firmware. The emulated GB200 NVL reports no manufacturer or
# model, so its model reads "- -". The lab boots build $old and this approves build $new, so make update updates it. To
# roll the lab back and forth, swap which line is commented.
"- -":
  "Manager (BMC)": "${versions[-1]}"  # build $new
  # "Manager (BMC)": "${versions[0]}"  # build $old, the one the lab boots
EOF
echo "Lab firmware: boots build $old (${versions[0]}), baseline approves build $new (${versions[-1]})"
