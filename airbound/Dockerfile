# Base images are pinned by digest (multi-arch index), so a re-pointed tag can't change what we build on.
FROM caddy:2.11.9-builder@sha256:f9beaa5ddbe7df6f26ebbcf9cbf44e05a1917b9bba464f0c3229038d7e2e6e13 AS caddy-build
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/hslatman/caddy-crowdsec-bouncer/http@v0.15.0 \
    --replace golang.org/x/net=golang.org/x/net@v0.62.0 \
    --replace golang.org/x/crypto=golang.org/x/crypto@v0.59.0 \
    --replace github.com/go-jose/go-jose/v4=github.com/go-jose/go-jose/v4@v4.1.9 \
    --replace github.com/klauspost/compress=github.com/klauspost/compress@v1.20.4 \
    --replace github.com/go-chi/chi/v5=github.com/go-chi/chi/v5@v5.4.0 \
    --output /usr/bin/caddy

FROM crowdsecurity/crowdsec:v1.8.2@sha256:65b74942f7e7ed5c60fde4ac15fc4ba715904c398c8b16dd451f4907d90347d4 AS crowdsec

FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS frpc
ARG BUILD_ARCH=amd64
ARG FRP_VERSION=0.72.0
# frp built from source with the newest Go by .github/workflows/frp.yaml
ARG FRP_RELEASE=frp-v0.72.0-airbound2
RUN case "$BUILD_ARCH" in \
      amd64) ARCH=amd64; SHA=74a228797e9796faaf6b60bab88cdde957f7269eb64c958b0568532a4105c105 ;; \
      aarch64) ARCH=arm64; SHA=ca84e4fcc8c29dbf81ecb9529c14e9f344cbddfada6150be104e0b16818f89ab ;; \
      *) echo "unsupported arch $BUILD_ARCH" && exit 1 ;; \
    esac \
    && wget -q -O /tmp/frp.tgz "https://github.com/oleost/AirBound/releases/download/${FRP_RELEASE}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz" \
    && echo "$SHA  /tmp/frp.tgz" | sha256sum -c - \
    && tar -xzf /tmp/frp.tgz -C /tmp \
    && mv /tmp/frp_${FRP_VERSION}_linux_${ARCH}/frpc /usr/local/bin/frpc

FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ARG BUILD_VERSION=dev
ENV AIRBOUND_VERSION=$BUILD_VERSION
COPY --from=caddy-build /usr/bin/caddy /usr/local/bin/caddy
COPY --from=frpc /usr/local/bin/frpc /usr/local/bin/frpc
COPY --from=crowdsec /usr/local/bin/crowdsec /usr/local/bin/cscli /usr/local/bin/
COPY --from=crowdsec /staging/etc/crowdsec /etc/crowdsec-default
COPY --from=crowdsec /staging/var/lib/crowdsec /var/lib/crowdsec-default
# Security fixes from Alpine since the base image was built (the Trivy scan checks this).
RUN apk upgrade --no-cache && apk add --no-cache tzdata
COPY requirements.txt /app/requirements.txt
# pip and setuptools are only needed to install; removing them drops their vendored packages from the image.
RUN pip install --no-cache-dir -r /app/requirements.txt && pip uninstall -y pip setuptools
COPY airbound /app/airbound
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    XDG_DATA_HOME=/data \
    XDG_CONFIG_HOME=/data
CMD ["python", "-m", "airbound"]
