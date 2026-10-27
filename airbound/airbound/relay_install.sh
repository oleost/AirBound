#!/bin/sh
# AirBound relay: passes encrypted traffic from the internet on to your Home Assistant.
# The relay never holds certificates or keys for your domain and can't read the traffic.
set -eu

TOKEN="__TOKEN__"
CONTROL_PORT="__CONTROL_PORT__"
PUBLIC_PORT="__PUBLIC_PORT__"
VERSION="0.71.1"
RELEASE="frp-v0.71.1-airbound1"  # frp built from source with the newest Go by AirBound's GitHub Actions

fail() { echo "AirBound relay: $1" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run this as root: log in as root, or install sudo."
command -v systemctl >/dev/null 2>&1 || fail "this server needs systemd (Debian or Ubuntu)."
command -v curl >/dev/null 2>&1 || fail "curl is missing. Install it with: apt install -y curl"

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64; SHA=5b054d744c4661bb87eae2eae2a88b2a930fb1fece50e4324c9f2ab9439e357f ;;
  aarch64 | arm64) ARCH=arm64; SHA=74fd646005fe79be5033d91a92ffb3e0694dd1497f9170eaeafcf7bcbc4a1790 ;;
  *) fail "unsupported processor: $(uname -m)" ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
NAME="frp_${VERSION}_linux_${ARCH}"
echo "Downloading the relay (frp $VERSION)..."
curl -fsSL -o "$TMP/frp.tgz" "https://github.com/oleost/HA-AirBound/releases/download/${RELEASE}/${NAME}.tar.gz"
echo "$SHA  $TMP/frp.tgz" | sha256sum -c - >/dev/null || fail "the download didn't match its checksum."
tar -xzf "$TMP/frp.tgz" -C "$TMP"
install -m 0755 "$TMP/$NAME/frps" /usr/local/bin/airbound-relay

id airbound >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin airbound
mkdir -p /etc/airbound
# The relay's certificate, made by your AirBound: AirBound only talks to this relay.
cat > /etc/airbound/relay.crt <<'CERT'
__SERVER_CERT__
CERT
cat > /etc/airbound/relay.key <<'KEY'
__SERVER_KEY__
KEY
cat > /etc/airbound/relay.toml <<EOF
bindPort = $CONTROL_PORT
auth.method = "token"
auth.token = "$TOKEN"
transport.tls.force = true
transport.tls.certFile = "/etc/airbound/relay.crt"
transport.tls.keyFile = "/etc/airbound/relay.key"
allowPorts = [{ single = $PUBLIC_PORT }]
log.to = "console"
log.level = "info"
EOF
chown root:airbound /etc/airbound/relay.toml /etc/airbound/relay.crt /etc/airbound/relay.key
chmod 0640 /etc/airbound/relay.toml /etc/airbound/relay.key
chmod 0644 /etc/airbound/relay.crt

cat > /etc/systemd/system/airbound-relay.service <<EOF
[Unit]
Description=AirBound relay for Home Assistant
After=network-online.target
Wants=network-online.target

[Service]
User=airbound
ExecStart=/usr/local/bin/airbound-relay -c /etc/airbound/relay.toml
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectClock=true
ProtectHostname=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictRealtime=true
RestrictNamespaces=true
RemoveIPC=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable airbound-relay >/dev/null 2>&1
systemctl restart airbound-relay

# A firewall, SSH keys only and automatic security updates (see vps_harden.sh in the add-on).
__HARDEN__
echo
harden_server "$CONTROL_PORT $PUBLIC_PORT"

sleep 1
systemctl is-active --quiet airbound-relay || fail "the relay didn't start. See: journalctl -u airbound-relay"
IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org || echo "this server's IP address")
echo
echo "AirBound relay is running."
echo "Go back to AirBound and enter this address: $IP"
