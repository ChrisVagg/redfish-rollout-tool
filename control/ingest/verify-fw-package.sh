#!/usr/bin/env bash
# The second of the lab's three ingest steps, between download-fw-package.sh and promote-store-fw-package.sh: checks
# each downloaded OpenBMC update package in the ingest's sandbox, the service sandbox in control/docker-compose.yaml:
# no network, a read-only root, as nobody, every capability dropped, control/ingest/ mounted read-only. A crafted
# package that breaks tar or openssl there reaches nothing: not the network, the store, Docker or the control plane's
# secrets. A package passes only if:
#   - it holds an OpenBMC package's files and nothing else, each a regular file, checked before anything is unpacked:
#     no path, link or device is ever written
#   - its MANIFEST, its key (publickey) and its image (image-bmc) each have an RSA-SHA256 signature, and every
#     signature in it verifies with the key pinned in openbmc-dev.pub, beside it, never with the key the package
#     carries, which anyone could replace together with the image. The pinned key is OpenBMC's development key, and its
#     private half is public in OpenBMC's source tree, so anyone can sign a modified package with it: in the lab the
#     checks show how verification works, they don't protect. Production pins the vendor's key, whose private half the
#     vendor keeps secret
#   - its MANIFEST, signed, says it's a BMC image built for the lab's machine, gb200nvl-obmc
# It writes nothing outside its scratch space. Only when every package passes does it print, on stdout, a line per
# package for promote-store-fw-package.sh: <package file name> <sha256> <version>
#   docker compose -f control/docker-compose.yaml run --rm -T sandbox > control/ingest/staging/verified
set -euo pipefail
key=$(dirname "$0")/openbmc-dev.pub
machine=gb200nvl-obmc  # the lab's BMCs: the build download-fw-package.sh downloads
# an OpenBMC static-flash package's files; image-full.sig, one signature over them together, is optional
allowed=" MANIFEST MANIFEST.sig publickey publickey.sig image-bmc image-bmc.sig image-full.sig "
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
[ $# -gt 0 ] || { echo "Usage: $0 PACKAGE.tar...: make control-up runs it in the sandbox" >&2; exit 1; }
refuse() { echo "$name: $*" >&2; exit 1; }

verified=()
for package in "$@"; do
  name=${package##*/}
  [ -f "$package" ] || refuse "no such package: download-fw-package.sh downloads them"
  # What it holds, before anything is unpacked: GNU tar lists type and mode, owner, size, date, time, then the name
  listing=$(tar -tvf "$package" 2> /dev/null) || refuse "not a tar archive"
  [ -z "$(tar -tf "$package" | sort | uniq -d)" ] || refuse "names a file twice"
  while read -r mode _ _ _ _ member; do
    [ "${mode:0:1}" = - ] || refuse "${member%% -> *} isn't a regular file"
    [[ $allowed == *" $member "* ]] || refuse "holds $member, which isn't an OpenBMC package's"
  done <<< "$listing"

  dir=$(mktemp -d -p "$work")
  tar -xf "$package" -C "$dir" --no-same-owner
  for f in MANIFEST publickey image-bmc; do  # each required, and signed: a signed MANIFEST doesn't sign the image
    [ -f "$dir/$f" ] && [ -f "$dir/$f.sig" ] || refuse "$f or its signature $f.sig is missing"
  done
  signed=0
  for sig in "$dir"/*.sig; do
    file=${sig%.sig}
    [ -f "$file" ] || continue  # image-full.sig signs the files together, not one file
    openssl dgst -sha256 -verify "$key" -signature "$sig" "$file" > /dev/null 2>&1 ||
      refuse "${file##*/}: its signature doesn't verify with ${key##*/}"
    signed=$((signed + 1))
  done

  # The MANIFEST's signature verified: what it says is now the package's
  field() { sed -n "s/^$1=//p" "$dir/MANIFEST"; }
  [ "$(field purpose)" = xyz.openbmc_project.Software.Version.VersionPurpose.BMC ] || refuse "isn't a BMC image"
  built=$(field MachineName)
  [ "$built" = "$machine" ] || refuse "built for ${built:-no machine}, not $machine"
  version=$(field version)
  [[ $version =~ ^[A-Za-z0-9._+-]+$ ]] || refuse "its MANIFEST has no version"
  echo "$name: $signed signatures verified with ${key##*/}, built for $machine" >&2
  verified+=("$name $(sha256sum < "$package" | cut -d' ' -f1) $version")
  rm -rf "$dir"
done
printf '%s\n' "${verified[@]}"
