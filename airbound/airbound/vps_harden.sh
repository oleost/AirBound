# Server hardening shared by AirBound's relay installers: the personal relay
# (embedded in the command the add-on shows) and the hosted relay (relay-server/).
#
#   harden_server "<TCP ports the relay needs>"
#
# - Firewall: only SSH, the relay's ports and whatever already listens publicly
#   get in. A server managed by ufw, firewalld or Oracle's iptables rules keeps
#   that firewall; the relay's ports are opened there instead.
# - SSH: password logins off, once the person running this has an SSH key.
# - Automatic security updates.
#
# Nothing here can lock you out: SSH and every service that is already reachable
# stay reachable, and SSH changes are only made when a key login exists.

harden_server() {
  harden_firewall "$1"
  harden_ssh
  harden_updates
}

harden_firewall() {
  if { command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; } || systemctl is-active --quiet firewalld 2>/dev/null; then
    # Another firewall took over since a previous run: two would fight over ports.
    if [ -f /etc/airbound-firewall.nft ]; then
      systemctl disable --now airbound-firewall >/dev/null 2>&1 || true
      rm -f /etc/airbound-firewall.nft /etc/systemd/system/airbound-firewall.service
      echo "Firewall: removed AirBound's own firewall, since another one now manages this server."
    fi
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    for P in $1; do ufw allow "$P/tcp" >/dev/null; done
    echo "Firewall: ufw manages this server; opened TCP $1 there."
    return 0
  fi
  if systemctl is-active --quiet firewalld 2>/dev/null; then
    for P in $1; do firewall-cmd --quiet --permanent --add-port="$P/tcp"; done
    firewall-cmd --quiet --reload
    echo "Firewall: firewalld manages this server; opened TCP $1 there."
    return 0
  fi
  if command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
    # Oracle Cloud's Ubuntu images end INPUT with a REJECT rule: the relay's
    # ports go first, then get saved so they survive a reboot.
    for P in $1; do
      iptables -C INPUT -p tcp --dport "$P" -m state --state NEW -j ACCEPT 2>/dev/null \
        || iptables -I INPUT 1 -p tcp --dport "$P" -m state --state NEW -j ACCEPT
    done
    if command -v netfilter-persistent >/dev/null 2>&1; then
      netfilter-persistent save >/dev/null 2>&1
    elif [ -d /etc/iptables ]; then
      iptables-save > /etc/iptables/rules.v4
    fi
    echo "Firewall: this server's iptables rules are in charge; opened TCP $1 there."
    return 0
  fi
  if ! command -v nft >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 && apt-get install -y -qq nftables >/dev/null 2>&1
    command -v nft >/dev/null 2>&1 || { echo "Firewall: nftables isn't available here, so no firewall was set up."; return 0; }
  fi
  # Everything that listens on a public address now stays reachable, SSH included.
  LISTEN_TCP=$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sed 's/.*://' | grep -E '^[0-9]+$' || true)
  LISTEN_UDP=$(ss -Huln 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sed 's/.*://' | grep -E '^[0-9]+$' || true)
  SSH_PORTS=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' || true)
  TCP=$(printf '%s\n' $1 $SSH_PORTS $LISTEN_TCP 22 | sort -un | paste -sd, -)
  UDP=$(printf '%s\n' $LISTEN_UDP | sort -un | paste -sd, -)
  UDP_RULE=""
  [ -n "$UDP" ] && UDP_RULE="udp dport { $UDP } accept"
  cat > /etc/airbound-firewall.nft <<EOF
# AirBound: only these ports get in. Made by the AirBound installer; run it again to update.
table inet airbound_firewall
delete table inet airbound_firewall
table inet airbound_firewall {
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		ct state invalid drop
		iif lo accept
		ip protocol icmp limit rate 20/second burst 50 packets accept
		ip6 nexthdr icmpv6 accept
		udp sport 67 udp dport 68 accept
		udp sport 547 udp dport 546 accept
		tcp dport { $TCP } accept
		$UDP_RULE
	}
}
EOF
  nft -c -f /etc/airbound-firewall.nft || { rm -f /etc/airbound-firewall.nft; echo "Firewall: the rules didn't check out, so no firewall was set up."; return 0; }
  cat > /etc/systemd/system/airbound-firewall.service <<'EOF'
[Unit]
Description=AirBound firewall (nftables)
After=nftables.service
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/airbound-firewall.nft
ExecStop=/usr/sbin/nft delete table inet airbound_firewall

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable airbound-firewall >/dev/null 2>&1
  systemctl restart airbound-firewall
  echo "Firewall: on. Open TCP ports: $TCP${UDP:+; UDP: $UDP}. Everything else is blocked."
}

harden_ssh() {
  command -v sshd >/dev/null 2>&1 || return 0
  WHO=${SUDO_USER:-root}
  KEYS="$(getent passwd "$WHO" | cut -d: -f6)/.ssh/authorized_keys"
  if [ ! -s "$KEYS" ]; then
    echo "SSH: $WHO logs in without an SSH key, so password logins stay on. Add a key and run this again to turn them off."
    return 0
  fi
  if ! grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config 2>/dev/null; then
    echo "SSH: left unchanged (this server's sshd_config doesn't read sshd_config.d)."
    return 0
  fi
  mkdir -p /etc/ssh/sshd_config.d
  # The first value sshd reads wins, and 10- comes before the cloud images' own 50- files.
  cat > /etc/ssh/sshd_config.d/10-airbound.conf <<'EOF'
# AirBound: SSH keys only. Remove this file to undo.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
X11Forwarding no
EOF
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    echo "SSH: key logins only; password logins are off."
  else
    rm -f /etc/ssh/sshd_config.d/10-airbound.conf
    echo "SSH: left unchanged (the new settings didn't pass sshd's own check)."
  fi
}

harden_updates() {
  command -v apt-get >/dev/null 2>&1 || return 0
  if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq unattended-upgrades >/dev/null 2>&1 || { echo "Updates: couldn't install unattended-upgrades."; return 0; }
  fi
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  # A kernel or library fix only takes effect after a restart; do it at night, when needed.
  printf 'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "04:30";\n' > /etc/apt/apt.conf.d/52airbound-reboot
  echo "Updates: security updates install automatically, with a restart at 04:30 when one needs it."
}
