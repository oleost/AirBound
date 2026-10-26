#!/bin/sh
# AirBound hosted relay: installs or updates the shared relay on a Debian/Ubuntu server.
#
#   sudo sh install.sh            (run from the relay-server directory)
#
# What runs afterwards:
#   airbound-hub         frps: routes HTTPS by name (SNI) to each Home Assistant, port 443 and 7000
#   airbound-controller  who may connect (frps plugin), Patreon login, the website's API
#   airbound-web         Caddy: the website on the domain itself, with its own certificate
#   airbound-site        frpc: publishes the website through the hub, like any Home Assistant
#
# Secrets live in /etc/airbound-controller and never in this repository.
set -eu

FRP_VERSION="0.71.0"
CADDY_VERSION="2.11.7"
HERE=$(cd "$(dirname "$0")" && pwd)

fail() { echo "AirBound hosted relay: $1" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "run this as root."
command -v systemctl >/dev/null 2>&1 || fail "this server needs systemd (Debian or Ubuntu)."
[ -d "$HERE/controller/airbound_relay" ] || fail "run this from the relay-server directory."

case "$(uname -m)" in
  x86_64 | amd64)
    ARCH=amd64
    FRP_SHA=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716
    CADDY_SHA=a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7
    ;;
  aarch64 | arm64)
    ARCH=arm64
    FRP_SHA=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266
    CADDY_SHA=3db36ba90c7a6e8dda40ee3dd71fa08844c76b5fb08f61b31e5e78d2ed38e71c51dc7baed875e50d1ca1279196e84302967237386ae87c91ae9f2aaceada682e
    ;;
  *) fail "unsupported processor: $(uname -m)" ;;
esac

echo "Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv openssl curl ca-certificates >/dev/null

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ "$(/usr/local/bin/airbound-hub --version 2>/dev/null || true)" != "$FRP_VERSION" ]; then
  echo "Downloading frp $FRP_VERSION..."
  NAME="frp_${FRP_VERSION}_linux_${ARCH}"
  curl -fsSL -o "$TMP/frp.tgz" "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${NAME}.tar.gz"
  echo "$FRP_SHA  $TMP/frp.tgz" | sha256sum -c - >/dev/null || fail "frp didn't match its checksum."
  tar -xzf "$TMP/frp.tgz" -C "$TMP"
  install -m 0755 "$TMP/$NAME/frps" /usr/local/bin/airbound-hub
  install -m 0755 "$TMP/$NAME/frpc" /usr/local/bin/airbound-site
fi

if ! /usr/local/bin/airbound-web version 2>/dev/null | grep -q "^v$CADDY_VERSION "; then
  echo "Downloading Caddy $CADDY_VERSION..."
  curl -fsSL -o "$TMP/caddy.tgz" "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${ARCH}.tar.gz"
  echo "$CADDY_SHA  $TMP/caddy.tgz" | sha512sum -c - >/dev/null || fail "Caddy didn't match its checksum."
  tar -xzf "$TMP/caddy.tgz" -C "$TMP" caddy
  install -m 0755 "$TMP/caddy" /usr/local/bin/airbound-web
fi

id airbound >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin airbound
id airbound-controller >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin airbound-controller

# Settings: created once, kept on updates.
CONF=/etc/airbound-controller
mkdir -p "$CONF"
if [ ! -f "$CONF/controller.env" ]; then
  PUBLIC_IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org || true)
  cat > "$CONF/controller.env" <<EOF
DOMAIN=airbound.dev
RELAY_HOST=relay.airbound.dev
PUBLIC_IP=$PUBLIC_IP
CAMPAIGN_ID=
RELAY_TIER_ID=
PATREON_VANITY=AirBound
FREE_PATREON_USERS=
MAX_DEVICES=2
BANDWIDTH=4MB
GRACE_DAYS=3
SITE_KEY=$(openssl rand -hex 24)
EOF
  echo "Created $CONF/controller.env: fill in CAMPAIGN_ID and RELAY_TIER_ID, then run this again."
fi
chown -R root:airbound-controller "$CONF"
chmod 0750 "$CONF"
chmod 0640 "$CONF"/*
. "$CONF/controller.env"
[ -n "$CAMPAIGN_ID" ] && [ -n "$RELAY_TIER_ID" ] || fail "fill in CAMPAIGN_ID and RELAY_TIER_ID in $CONF/controller.env first."
[ -f "$CONF/patreon.env" ] || fail "put the Patreon client details in $CONF/patreon.env first."

# The hub's own certificate. Home Assistant only trusts this CA, so nobody can pose as the relay.
HUB=/etc/airbound-hub
mkdir -p "$HUB"
if [ ! -f "$HUB/ca.crt" ]; then
  echo "Creating the relay's certificate authority..."
  openssl ecparam -name prime256v1 -genkey -noout -out "$TMP/ca.key"
  openssl req -x509 -new -key "$TMP/ca.key" -sha256 -days 7300 -subj "/CN=AirBound hosted relay CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" -out "$HUB/ca.crt"
  openssl ecparam -name prime256v1 -genkey -noout -out "$HUB/server.key"
  openssl req -new -key "$HUB/server.key" -subj "/CN=airbound-hosted-relay" -out "$TMP/server.csr"
  printf "subjectAltName=DNS:airbound-hosted-relay\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n" > "$TMP/ext"
  openssl x509 -req -in "$TMP/server.csr" -CA "$HUB/ca.crt" -CAkey "$TMP/ca.key" -CAcreateserial -days 7300 -sha256 \
    -extfile "$TMP/ext" -out "$HUB/server.crt" 2>/dev/null
  # The CA key is only needed to sign the server certificate; it doesn't stay on the server.
  rm -f "$TMP/ca.key" "$HUB/ca.srl"
fi

cat > "$HUB/frps.toml" <<EOF
bindPort = 7000
vhostHTTPSPort = 443
# Not a secret: every client is checked by the controller (httpPlugins below).
auth.method = "token"
auth.token = "airbound-hosted-relay"
transport.tls.force = true
transport.tls.certFile = "$HUB/server.crt"
transport.tls.keyFile = "$HUB/server.key"
transport.maxPoolCount = 5
maxPortsPerClient = 1
allowPorts = [{ single = 1 }]
log.to = "console"
log.level = "info"

[[httpPlugins]]
name = "airbound"
addr = "127.0.0.1:7600"
path = "/frp"
ops = ["Login", "NewProxy", "Ping"]
EOF

cat > "$HUB/site.toml" <<EOF
serverAddr = "127.0.0.1"
serverPort = 7000
user = "site"
metadatas.key = "$SITE_KEY"
auth.method = "token"
auth.token = "airbound-hosted-relay"
transport.tls.enable = true
transport.tls.trustedCaFile = "$HUB/ca.crt"
transport.tls.serverName = "airbound-hosted-relay"
loginFailExit = false
log.to = "console"
log.level = "warn"

[[proxies]]
name = "website"
type = "https"
customDomains = ["$DOMAIN"]
localIP = "127.0.0.1"
localPort = 8443
transport.proxyProtocolVersion = "v2"
EOF

cat > "$HUB/Caddyfile" <<EOF
{
	admin off
	auto_https disable_redirects
	storage file_system /var/lib/airbound-web
	cert_issuer acme {
		disable_http_challenge
	}
	servers 127.0.0.1:8443 {
		protocols h1 h2
		listener_wrappers {
			proxy_protocol {
				allow 127.0.0.1/32
			}
			tls
		}
	}
}

https://$DOMAIN:8443 {
	bind 127.0.0.1
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:7500
}
EOF

chown -R root:airbound "$HUB"
chmod 0755 "$HUB"
chmod 0644 "$HUB"/*
chmod 0640 "$HUB/server.key" "$HUB/site.toml"  # the hub's key and the website's relay key
mkdir -p /var/lib/airbound-web
chown airbound:airbound /var/lib/airbound-web

echo "Installing the controller..."
APP=/opt/airbound-controller
mkdir -p "$APP"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$HERE/controller/requirements.txt"
rm -rf "$APP/airbound_relay"
cp -r "$HERE/controller/airbound_relay" "$APP/"
mkdir -p /var/lib/airbound-controller
chown airbound-controller:airbound-controller /var/lib/airbound-controller
chmod 0700 /var/lib/airbound-controller

HARDENING="NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
Restart=always
RestartSec=5"

cat > /etc/systemd/system/airbound-controller.service <<EOF
[Unit]
Description=AirBound hosted relay controller
After=network-online.target
Wants=network-online.target

[Service]
User=airbound-controller
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m airbound_relay
ReadWritePaths=/var/lib/airbound-controller
$HARDENING

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/airbound-hub.service <<EOF
[Unit]
Description=AirBound hosted relay hub (frps)
After=network-online.target airbound-controller.service
Wants=network-online.target

[Service]
User=airbound
ExecStart=/usr/local/bin/airbound-hub -c $HUB/frps.toml
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
$HARDENING

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/airbound-web.service <<EOF
[Unit]
Description=AirBound website (Caddy)
After=network-online.target

[Service]
User=airbound
Environment=HOME=/var/lib/airbound-web XDG_DATA_HOME=/var/lib/airbound-web XDG_CONFIG_HOME=/var/lib/airbound-web
ExecStart=/usr/local/bin/airbound-web run --config $HUB/Caddyfile --adapter caddyfile
ReadWritePaths=/var/lib/airbound-web
$HARDENING

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/airbound-site.service <<EOF
[Unit]
Description=AirBound website tunnel (frpc)
After=airbound-hub.service

[Service]
User=airbound
ExecStart=/usr/local/bin/airbound-site -c $HUB/site.toml
$HARDENING

[Install]
WantedBy=multi-user.target
EOF

# Connection logs (who connected when) are kept for 14 days, as the privacy page says.
mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nMaxRetentionSec=14day\n" > /etc/systemd/journald.conf.d/airbound.conf
systemctl restart systemd-journald

# A personal relay from the add-on's own relay mode uses port 7000 too; the hub replaces it.
if systemctl list-unit-files airbound-relay.service >/dev/null 2>&1 && systemctl is-enabled --quiet airbound-relay 2>/dev/null; then
  echo "Replacing the personal relay (airbound-relay) with the hosted relay..."
  systemctl disable --now airbound-relay >/dev/null 2>&1 || true
fi

systemctl daemon-reload
for UNIT in airbound-controller airbound-hub airbound-web airbound-site; do
  systemctl enable "$UNIT" >/dev/null 2>&1
  systemctl restart "$UNIT"
done

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 443/tcp >/dev/null
  ufw allow 7000/tcp >/dev/null
fi

sleep 2
for UNIT in airbound-controller airbound-hub airbound-web airbound-site; do
  systemctl is-active --quiet "$UNIT" || fail "$UNIT didn't start. See: journalctl -u $UNIT"
done
echo "AirBound hosted relay is running for $DOMAIN."
echo "Point the domain here once with: cd $APP && sudo -u airbound-controller $APP/venv/bin/python -m airbound_relay dns"
