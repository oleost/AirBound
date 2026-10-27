# Base images are pinned by digest (multi-arch index), so a re-pointed tag can't change what we build on.
FROM caddy:2.11.7-builder@sha256:c2fc374fd352439d76e1777b28d97d9f6d9c1dd86116db9227460374d46097d5 AS caddy-build
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/hslatman/caddy-crowdsec-bouncer/http@v0.14.1 \
    --replace github.com/crowdsecurity/crowdsec=github.com/crowdsecurity/crowdsec@v1.7.8 \
    --replace golang.org/x/net=golang.org/x/net@v0.60.0 \
    --replace github.com/go-chi/chi/v5=github.com/go-chi/chi/v5@v5.4.0 \
    --output /usr/bin/caddy

FROM crowdsecurity/crowdsec:v1.8.1@sha256:0f2523fa61ef507f15d953045cface490cc880670c62f2755ced17524107f71a AS crowdsec

FROM python:3.13.18-alpine3.24@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48 AS frpc
ARG BUILD_ARCH=amd64
ARG FRP_VERSION=0.71.1
RUN case "$BUILD_ARCH" in \
      amd64) ARCH=amd64; SHA=1d9875210972a5d1a62b2dc4c48df45ab0af05d3ca1b5e494a014428259df803 ;; \
      aarch64) ARCH=arm64; SHA=8edf590b0c396fa42f9c147121a76af83cfc2002b08f5aef0ea1008c43482019 ;; \
      *) echo "unsupported arch $BUILD_ARCH" && exit 1 ;; \
    esac \
    && wget -q -O /tmp/frp.tgz "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz" \
    && echo "$SHA  /tmp/frp.tgz" | sha256sum -c - \
    && tar -xzf /tmp/frp.tgz -C /tmp \
    && mv /tmp/frp_${FRP_VERSION}_linux_${ARCH}/frpc /usr/local/bin/frpc

FROM python:3.13.18-alpine3.24@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48
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
