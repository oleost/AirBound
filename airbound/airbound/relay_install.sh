#!/bin/sh
# AirBound relay: passes encrypted traffic from the internet on to your Home Assistant.
# The relay never holds certificates or keys for your domain and can't read the traffic.
set -eu

TOKEN="__TOKEN__"
CONTROL_PORT="__CONTROL_PORT__"
PUBLIC_PORT="__PUBLIC_PORT__"
VERSION="0.71.0"

fail() { echo "AirBound relay: $1" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run this as root (put sudo in front)."
command -v systemctl >/dev/null 2>&1 || fail "this server needs systemd (Debian or Ubuntu)."
command -v curl >/dev/null 2>&1 || fail "curl is missing. Install it with: apt install -y curl"

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64; SHA=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716 ;;
  aarch64 | arm64) ARCH=arm64; SHA=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266 ;;
  *) fail "unsupported processor: $(uname -m)" ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
NAME="frp_${VERSION}_linux_${ARCH}"
echo "Downloading the relay (frp $VERSION)..."
curl -fsSL -o "$TMP/frp.tgz" "https://github.com/fatedier/frp/releases/download/v${VERSION}/${NAME}.tar.gz"
echo "$SHA  $TMP/frp.tgz" | sha256sum -c - >/dev/null || fail "the download didn't match its checksum."
tar -xzf "$TMP/frp.tgz" -C "$TMP"
install -m 0755 "$TMP/$NAME/frps" /usr/local/bin/airbound-relay

id airbound >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin airbound
mkdir -p /etc/airbound
cat > /etc/airbound/relay.toml <<EOF
bindPort = $CONTROL_PORT
auth.method = "token"
auth.token = "$TOKEN"
transport.tls.force = true
allowPorts = [{ single = $PUBLIC_PORT }]
log.to = "console"
log.level = "info"
EOF
chown root:airbound /etc/airbound/relay.toml
chmod 0640 /etc/airbound/relay.toml

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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable airbound-relay >/dev/null 2>&1
systemctl restart airbound-relay

# Open the two ports in whichever firewall the server uses.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$CONTROL_PORT/tcp" >/dev/null
  ufw allow "$PUBLIC_PORT/tcp" >/dev/null
elif systemctl is-active --quiet firewalld 2>/dev/null; then
  # Oracle Linux, Rocky, Fedora
  firewall-cmd --quiet --permanent --add-port="$CONTROL_PORT/tcp" --add-port="$PUBLIC_PORT/tcp"
  firewall-cmd --quiet --reload
elif command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
  # Oracle Cloud's Ubuntu images: iptables ends INPUT with a REJECT rule, so
  # the new rules go first, then get saved so they survive a reboot.
  for PORT in "$CONTROL_PORT" "$PUBLIC_PORT"; do
    iptables -C INPUT -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 1 -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1
  elif [ -d /etc/iptables ]; then
    iptables-save > /etc/iptables/rules.v4
  fi
fi

sleep 1
systemctl is-active --quiet airbound-relay || fail "the relay didn't start. See: journalctl -u airbound-relay"
IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org || echo "this server's IP address")
echo
echo "AirBound relay is running."
echo "Go back to AirBound and enter this address: $IP"
