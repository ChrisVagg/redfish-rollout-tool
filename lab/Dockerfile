# OpenBMC's GB200 NVL image run by OpenBMC's prebuilt QEMU (full-system emulation of the AST2600 BMC chip).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libpixman-1-0 libslirp0 libfdt1 zlib1g curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY qemu-system-arm bmc.mtd entrypoint.sh /opt/openbmc/
EXPOSE 443 22
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3 \
  CMD curl -skf -o /dev/null https://127.0.0.1/redfish/v1 || exit 1
ENTRYPOINT ["/opt/openbmc/entrypoint.sh"]
