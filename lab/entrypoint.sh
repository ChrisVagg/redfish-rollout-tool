#!/bin/sh
# Boot the emulated BMC from its own flash: a fresh copy of the image the first time.
set -e
[ -f /flash/bmc.mtd ] || cp /opt/openbmc/bmc.mtd /flash/bmc.mtd
exec /opt/openbmc/qemu-system-arm -M gb200nvl-bmc -nographic \
  -drive file=/flash/bmc.mtd,format=raw,if=mtd \
  -net nic -net user,hostfwd=tcp::443-:443,hostfwd=tcp::22-:22,hostname=bmc
