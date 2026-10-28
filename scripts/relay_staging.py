"""Relay staging: install relay-server/ on the fresh test VPS before it goes near airbound.dev.

    python scripts/relay_staging.py

Installs the hosted relay with staging settings (no real Patreon or
Cloudflare), then checks: every service runs inside its sandbox, the firewall
is up, the frp plugin refuses fake clients and a real key from another machine
(a copied backup), the controller reads the hub's
traffic counters, and the health endpoint answers. The VPS is stopped
afterwards. The result goes to test-results/relay-staging-<commit>.json, which
scripts/release.py requires when relay-server/ changed.
"""

from __future__ import annotations

import json
import re
import secrets
import sys

from testenv import ROOT, TestVPS, begin_run, relay_tarball, save_result

UNITS = ("airbound-controller", "airbound-hub", "airbound-web", "airbound-site")
MAX_EXPOSURE = 2.5  # systemd-analyze security; the units score about 1.7-1.9

STAGING_ENV = f"""DOMAIN=staging.airbound.test
RELAY_HOST=relay.staging.airbound.test
PUBLIC_IP=
CAMPAIGN_ID=0
RELAY_TIER_ID=0
PATREON_VANITY=AirBound
FREE_PATREON_USERS=
MAX_DEVICES=2
BANDWIDTH=4MB
GRACE_DAYS=3
SITE_KEY={secrets.token_hex(24)}
"""

# A fake frp client: (description, extra config lines, what the relay must answer).
PROBES = [
    ("no key, publishing the website's name", ['user = "site"', "[[proxies]]", 'name = "x"', 'type = "https"', 'customDomains = ["staging.airbound.test"]', "localPort = 1"], "unknown device"),
    ("made-up device and key", ['user = "dEVIL"', 'metadatas.key = "x"', "[[proxies]]", 'name = "x"', 'type = "https"', 'customDomains = ["abc.staging.airbound.test"]', "localPort = 1"], "unknown device"),
    ("made-up device asking for a TCP port", ['user = "dEVIL"', 'metadatas.key = "x"', "[[proxies]]", 'name = "x"', 'type = "tcp"', "remotePort = 2222", "localPort = 1"], "unknown device"),
]


# A real device for the machine check, made in the controller's own database.
MAKE_DEVICE = """
from airbound_relay.db import Database
db = Database("/var/lib/airbound-controller/controller.db")
db.set_entitled("staging-probe", True)
device, key = db.link_device("staging-probe", "staging-probe-install-0001", 2)
print(device.id, key, device.hostname)
"""


def controller_python(vps: TestVPS, code: str) -> str:
    vps.sh(f"cat > /tmp/probe.py <<'EOF'\n{code}\nEOF\nchmod 644 /tmp/probe.py")
    out = vps.sh("cd /opt/airbound-controller && sudo -u airbound-controller env PYTHONPATH=/opt/airbound-controller "
                 "venv/bin/python /tmp/probe.py 2>&1; rm -f /tmp/probe.py")
    return out


def device_probe(device: str, key: str, hostname: str, machine: str) -> list[str]:
    """machine "env": read from AIRBOUND_MACHINE, the way the add-on's frpc config does."""
    value = "{{ .Envs.AIRBOUND_MACHINE }}" if machine == "env" else machine
    return [f'user = "{device}"', f'metadatas.key = "{key}"', f'metadatas.machine = "{value}"', "[[proxies]]", 'name = "x"',
            'type = "https"', f'customDomains = ["{hostname}.staging.airbound.test"]', "localPort = 1"]


def probe(vps: TestVPS, lines: list[str], environment: str = "") -> str:
    config = "\n".join(['serverAddr = "127.0.0.1"', "serverPort = 7000", 'auth.method = "token"', 'auth.token = "airbound-hosted-relay"',
                        "transport.tls.enable = true", 'transport.tls.trustedCaFile = "/etc/airbound-hub/ca.crt"',
                        'transport.tls.serverName = "airbound-hosted-relay"', "loginFailExit = true", *lines])
    vps.sh(f"cat > /tmp/probe.toml <<'EOF'\n{config}\nEOF")
    return vps.sh(f"{environment} timeout 8 /usr/local/bin/airbound-site -c /tmp/probe.toml 2>&1 | tail -3; rm -f /tmp/probe.toml", check=False)


def main() -> None:
    print(f"testing {begin_run()[:12]}")
    results: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: str) -> None:
        results[name] = {"ok": ok, "detail": detail}
        print(f"{'✓' if ok else '✗'} {name}: {detail}")

    with TestVPS() as vps:
        vps.put(relay_tarball(), "relay.tgz")
        vps.sh("sudo mkdir -p /etc/airbound-controller && sudo chmod 700 /etc/airbound-controller")
        vps.sh(f"sudo tee /etc/airbound-controller/controller.env >/dev/null <<'EOF'\n{STAGING_ENV}EOF")
        vps.sh("printf 'PATREON_CLIENT_ID=staging\\nPATREON_CLIENT_SECRET=staging\\n' | sudo tee /etc/airbound-controller/patreon.env >/dev/null")
        out = vps.sh("cd ~ && rm -rf relay-server airbound && tar xzf relay.tgz && cd relay-server && sudo sh install.sh 2>&1 | tail -12", check=False, timeout=1200)
        check("install", "AirBound hosted relay is running" in out, out.strip().splitlines()[-2] if out.strip() else "no output")

        states = {u: vps.sh(f"systemctl is-active {u}", check=False).strip() for u in UNITS}
        check("services", all(s == "active" for s in states.values()), ", ".join(f"{u.removeprefix('airbound-')} {s}" for u, s in states.items()))

        scores = {}
        for unit in UNITS:
            line = vps.sh(f"systemd-analyze security {unit} --no-pager 2>/dev/null | tail -1", check=False)
            match = re.search(r"(\d+\.\d+)\s+(?:OK|MEDIUM|EXPOSED|UNSAFE)", line)  # the line ends in a smiley with a colon
            scores[unit.removeprefix("airbound-")] = float(match.group(1)) if match else 99.0
        check("sandbox", all(v <= MAX_EXPOSURE for v in scores.values()), json.dumps(scores))

        firewall = vps.sh("sudo nft list table inet airbound_firewall 2>/dev/null | grep -c 'policy drop'", check=False).strip()
        check("firewall", firewall == "1", "drop policy active" if firewall == "1" else "airbound_firewall table missing")

        for description, lines, expected in PROBES:
            answer = probe(vps, lines)
            check(f"refuses: {description}", expected in answer, answer.strip().splitlines()[-1][:120] if answer.strip() else "no answer")

        made = controller_python(vps, MAKE_DEVICE).split()
        if len(made) != 3:
            raise RuntimeError(f"couldn't make a test device: {' '.join(made)[-400:]}")
        device, key, hostname = made
        first = probe(vps, device_probe(device, key, hostname, "env"), environment="AIRBOUND_MACHINE=home")
        check("accepts: a device's key from its own machine", "start proxy success" in first, first.strip().splitlines()[-1][:120] if first.strip() else "no answer")
        copied = probe(vps, device_probe(device, key, hostname, "copy"))
        check("refuses: the same key from another machine", "other machine" in copied, copied.strip().splitlines()[-1][:120] if copied.strip() else "no answer")

        meter = vps.sh("sudo sh -c '. /etc/airbound-controller/controller.env; curl -s -u controller:$HUB_ADMIN_PASSWORD http://127.0.0.1:7400/api/proxy/https'", check=False)
        try:
            names = [p["name"] for p in json.loads(meter).get("proxies") or []]
            check("traffic counters", "site.website" in names, f"hub admin API lists {names}")
        except ValueError:
            check("traffic counters", False, meter[:200])

        ops = vps.sh("sudo sh -c '. /etc/airbound-controller/controller.env; curl -s -H \"Authorization: Bearer $OPS_TOKEN\" http://127.0.0.1:7500/api/v1/ops'", check=False)
        try:
            data = json.loads(ops)
            check("health endpoint", bool(data.get("frp_build")), f"frp {data.get('frp_build')}, controller {str(data.get('controller_build')).splitlines()[0]}")
        except ValueError:
            check("health endpoint", False, ops[:200])

    path = save_result("relay-staging", results)
    failed = [k for k, v in results.items() if not v["ok"]]
    print(f"\n{'FAILED: ' + ', '.join(failed) if failed else 'PASSED'}; result in {path.relative_to(ROOT)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
