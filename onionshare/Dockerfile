ARG BUILD_FROM=ghcr.io/hassio-addons/base:15.0.7
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System packages
# - tor: the Tor daemon
# - py3-* core build deps
# - Headers we need for some onionshare-cli wheels
RUN apk add --no-cache \
        tor \
        python3 \
        py3-pip \
        py3-cryptography \
        py3-pynacl \
        py3-cffi \
        py3-stem \
        py3-flask \
        py3-psutil \
        py3-requests \
        py3-yaml \
        py3-qrcode \
        ca-certificates \
        curl \
        bash \
        && rm -rf /var/cache/apk/*

# Install onionshare-cli (and its deps that aren't already provided by apk)
# --break-system-packages because Alpine py3 is "externally managed"
RUN pip3 install --no-cache-dir --break-system-packages \
        onionshare-cli \
        flask \
        werkzeug

# Create dirs Tor and onionshare expect
RUN mkdir -p /var/lib/tor /var/log/tor /data/onionshare \
    && chown -R tor:tor /var/lib/tor /var/log/tor

# Copy our Flask app
COPY app /opt/onionshare-addon/app

# Copy s6 rootfs (services, init scripts, configs)
COPY rootfs /

EXPOSE 8099

LABEL \
    io.hass.name="OnionShare" \
    io.hass.description="Share & receive files anonymously via Tor" \
    io.hass.arch="aarch64|amd64" \
    io.hass.type="addon" \
    io.hass.version="0.1.2" \
    maintainer="gregorwolf1973"
