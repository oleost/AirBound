"""Deploy relay-server/ to the live hosted relay, putting the old one back if it fails.

    python scripts/relay_deploy.py [member URL] [--simulate-failure]

--simulate-failure exercises the rollback: it treats the deploy as failed.

1. Keeps a copy of everything the installer replaces (controller, hub config,
   binaries, systemd units, firewall rules) in /var/backups/airbound.
2. Arms a firewall safety net: if this script loses the server, the AirBound
   firewall and SSH settings are removed again after 10 minutes.
3. Runs the installer, then checks the services, the website, a member's
   address and the health endpoint for up to two minutes.
4. On failure, restores the copy and restarts everything, then checks again.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from testenv import env, relay_tarball, scp, ssh

UNITS = "airbound-controller airbound-hub airbound-web airbound-site"
BACKUP_PATHS = ("/opt/airbound-controller/airbound_relay /opt/airbound-controller/BUILD /etc/airbound-hub /usr/local/bin/airbound-hub "
                "/usr/local/bin/airbound-site /usr/local/bin/airbound-web /usr/local/lib/airbound-frp-build /etc/systemd/system/airbound-*.service "
                "/etc/airbound-firewall.nft /etc/ssh/sshd_config.d/10-airbound.conf")
SAFETY_NET = ("systemd-run --quiet --unit=airbound-deploy-safety --on-active=600 sh -c "
              "'nft delete table inet airbound_firewall; systemctl disable airbound-firewall; rm -f /etc/ssh/sshd_config.d/10-airbound.conf; systemctl reload ssh'")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:
        return 0


def healthy(target: str, key: str, domain: str, member_url: str | None) -> tuple[bool, str]:
    states = ssh(target, key, f"for u in {UNITS}; do systemctl is-active $u; done", check=False).split()
    if states != ["active"] * 4:
        return False, f"services: {states}"
    if http_status(f"https://{domain}/") != 200:
        return False, f"https://{domain}/ answers {http_status(f'https://{domain}/')}"
    if member_url and http_status(member_url) != 200:
        return False, f"{member_url} answers {http_status(member_url)}"
    ops = ssh(target, key, f". /etc/airbound-controller/controller.env; curl -s -o /dev/null -w '%{{http_code}}' -H \"Authorization: Bearer $OPS_TOKEN\" https://{domain}/api/v1/ops", check=False)
    if ops.strip() != "200":
        return False, f"health endpoint answers {ops.strip()}"
    return True, "services, website, member address and health endpoint OK"


def wait_healthy(target: str, key: str, domain: str, member_url: str | None, seconds: int = 120) -> tuple[bool, str]:
    reason = ""
    for _ in range(seconds // 10):
        ok, reason = healthy(target, key, domain, member_url)
        if ok:
            return True, reason
        time.sleep(10)
    return False, reason


def main() -> None:
    target, key, domain = env("RELAY_SSH"), env("RELAY_KEY"), env("RELAY_DOMAIN")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    member_url = args[0] if args else None  # e.g. the test VM's hosted address
    simulate_failure = "--simulate-failure" in sys.argv
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ok, reason = healthy(target, key, domain, member_url)
    if not ok:
        raise SystemExit(f"The relay isn't healthy before the deploy ({reason}); fix that first.")

    print(f"backing up to /var/backups/airbound/{stamp}.tgz")
    ssh(target, key, f"mkdir -p /var/backups/airbound && tar czf /var/backups/airbound/{stamp}.tgz {BACKUP_PATHS} 2>/dev/null; "
                     f"ls /var/backups/airbound/*.tgz | head -n -5 | xargs -r rm -f")  # keep the last five
    ssh(target, key, SAFETY_NET)
    scp(key, str(relay_tarball()), f"{target}:/root/relay.tgz")
    out = ssh(target, key, "cd /root && rm -rf relay-server airbound && tar xzf relay.tgz && cd relay-server && sh install.sh 2>&1 | tail -4", check=False, timeout=1200)
    print(out.strip())

    ok, reason = wait_healthy(target, key, domain, member_url)
    if simulate_failure:
        ok, reason = False, "simulated failure (--simulate-failure)"
    if ok:
        ssh(target, key, "systemctl stop airbound-deploy-safety.timer 2>/dev/null; true")
        print(f"DEPLOYED: {reason}")
        return

    print(f"ROLLING BACK: {reason}")
    ssh(target, key, f"cd / && tar xzf /var/backups/airbound/{stamp}.tgz && systemctl daemon-reload && "
                     f"systemctl restart airbound-firewall {UNITS}", check=False)
    ok, after = wait_healthy(target, key, domain, member_url)
    ssh(target, key, "systemctl stop airbound-deploy-safety.timer 2>/dev/null; true")
    raise SystemExit(f"Deploy failed ({reason}); rolled back: {'healthy again' if ok else 'STILL UNHEALTHY: ' + after}")


if __name__ == "__main__":
    main()
