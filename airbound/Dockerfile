FROM caddy:2-builder AS caddy-build
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare \
    --with github.com/hslatman/caddy-crowdsec-bouncer/http@v0.14.1 \
    --output /usr/bin/caddy

FROM crowdsecurity/crowdsec:v1.8.1 AS crowdsec

FROM python:3.13-alpine AS frpc
ARG BUILD_ARCH=amd64
ARG FRP_VERSION=0.71.0
RUN case "$BUILD_ARCH" in \
      amd64) ARCH=amd64; SHA=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716 ;; \
      aarch64) ARCH=arm64; SHA=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266 ;; \
      *) echo "unsupported arch $BUILD_ARCH" && exit 1 ;; \
    esac \
    && wget -q -O /tmp/frp.tgz "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz" \
    && echo "$SHA  /tmp/frp.tgz" | sha256sum -c - \
    && tar -xzf /tmp/frp.tgz -C /tmp \
    && mv /tmp/frp_${FRP_VERSION}_linux_${ARCH}/frpc /usr/local/bin/frpc

FROM python:3.13-alpine
COPY --from=caddy-build /usr/bin/caddy /usr/local/bin/caddy
COPY --from=frpc /usr/local/bin/frpc /usr/local/bin/frpc
COPY --from=crowdsec /usr/local/bin/crowdsec /usr/local/bin/cscli /usr/local/bin/
COPY --from=crowdsec /staging/etc/crowdsec /etc/crowdsec-default
COPY --from=crowdsec /staging/var/lib/crowdsec /var/lib/crowdsec-default
RUN apk add --no-cache tzdata
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY airbound /app/airbound
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    XDG_DATA_HOME=/data \
    XDG_CONFIG_HOME=/data
CMD ["python", "-m", "airbound"]
