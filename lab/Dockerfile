# OpenBMC's GB200 NVL image run by OpenBMC's prebuilt QEMU (full-system emulation of the AST2600 BMC chip).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libpixman-1-0 libslirp0 libfdt1 zlib1g curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY qemu-system-arm bmc.mtd entrypoint.sh /opt/openbmc/
EXPOSE 443 22
# Healthy once the Manager reports Enabled: booted, and not degraded. Now and then a slow boot runs a service that writes
# the U-Boot environment before /dev/mtd/u-boot-env exists: systemd ends degraded and the Manager reports Quiesced. Then
# the check stops QEMU, PID 1, and Docker's restart policy boots the BMC again: a fresh boot fixes it
HEALTHCHECK --interval=15s --timeout=10s --start-period=600s --retries=3 CMD \
  state=$(curl -sk -m 8 -u root:0penBmc https://127.0.0.1/redfish/v1/Managers/bmc | grep -o '"State": "[A-Za-z]*"'); \
  case "$state" in *Enabled*) exit 0 ;; *Quiesced*) kill 1 ;; esac; exit 1
ENTRYPOINT ["/opt/openbmc/entrypoint.sh"]
