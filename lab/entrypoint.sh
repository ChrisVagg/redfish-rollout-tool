#!/bin/sh
# Boot the emulated BMC from its own flash: a fresh copy of the image the first time,
# then whatever an update from redfsih service wrote.
set -e
[ -f /flash/bmc.mtd ] || cp /opt/openbmc/bmc.mtd /flash/bmc.mtd
# Ten emulated BMCs booting at once starve each other of CPU, and a slow boot fails some of their services (the
# u-boot-env device isn't there in time): docker-compose.yaml starts them in two batches
sleep "${BOOT_DELAY:-0}"
exec /opt/openbmc/qemu-system-arm -M gb200nvl-bmc -nographic \
  -drive file=/flash/bmc.mtd,format=raw,if=mtd \
  -net nic -net user,hostfwd=tcp::443-:443,hostfwd=tcp::22-:22,hostname=bmc
