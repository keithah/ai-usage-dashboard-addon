# Home Assistant add-on image for AI Usage Dashboard.
# Explicit base image, per Supervisor >= 2026.04.0 requirements.
# Built by the Supervisor (or `docker build`) with the add-on directory
# as build context.
FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64

LABEL io.hass.name="AI Usage Dashboard" \
    io.hass.description="Poll AI provider usage and quota and publish Home Assistant MQTT discovery sensors." \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

WORKDIR /app

# The base image does not guarantee a Python runtime, so install it
# explicitly along with pip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Vendored copy of the collector package (kept in sync with the repo root
# ai_usage_dashboard/ directory; tests/test_addon.py enforces equality).
#
# NOTE: this image does not bundle the provider CLIs (`bl`, `opencode`,
# `muse`). Alibaba Coding Plan polling needs the official `bl` CLI: mount
# it into the container and set the account's `cli_path` option to its
# path, or extend this image with a pinned/bundled copy in your own fork.
# Without the CLI the account reports a clean `unsupported` status.
COPY rootfs/app/ai_usage_dashboard ./ai_usage_dashboard

RUN pip3 install --no-cache-dir --break-system-packages paho-mqtt pyyaml \
    && python3 -c "import ai_usage_dashboard.addon_options" \
    && rm -rf /root/.cache

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
