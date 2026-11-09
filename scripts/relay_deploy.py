"""Deploy relay-server/ to the live hosted relay, putting the old one back if it fails.

    python scripts/relay_deploy.py [member URL] [--simulate-failure]

--simulate-failure exercises the rollback: it treats the deploy as failed.

1. Keeps a copy of everything the installer replaces (controller, hub config,
   binaries, systemd units, firewall rules) in /var/backups/airbound.
2. Arms a firewall safety net: if this script loses the server, the AirBound
   firewall and SSH settings are removed again after 10 minutes. Everything
   runs over one SSH connection, so the deploy doesn't look like a scan.
3. Runs the installer, then checks the services, the website, a member's
   address and the health endpoint for up to two minutes.
4. On failure, restores the copy and restarts everything, then checks again.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from testenv import Remote, env, relay_tarball

UNITS = "airbound-controller airbound-hub airbound-web airbound-site"
BACKUP_PATHS = ("/opt/airbound-controller/airbound_relay /opt/airbound-controller/BUILD /etc/airbound-hub /usr/local/bin/airbound-hub "
                "/usr/local/bin/airbound-site /usr/local/bin/airbound-web /usr/local/lib/airbound-frp-build /etc/systemd/system/airbound-*.service "
                "/etc/airbound-firewall.nft /etc/ssh/sshd_config.d/10-airbound.conf")
# Stopping the unit (not just deleting its table) lets the installer's "enable --now" load the firewall again.
def safety_net(seconds: int = 600) -> str:
    return (f"systemd-run --quiet --unit=airbound-deploy-safety --on-active={seconds} sh -c "
            "'systemctl disable --now airbound-firewall; nft delete table inet airbound_firewall 2>/dev/null; "
            "rm -f /etc/ssh/sshd_config.d/10-airbound.conf; systemctl reload ssh'")


class DeployFailed(Exception):
    pass


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:
        return 0


def healthy(remote: Remote, domain: str | None, member_url: str | None) -> tuple[bool, str]:
    """domain None: a relay without a public name (the staging VPS); the website and health checks then ask the controller on the server."""
    states = remote.run(f"for u in {UNITS}; do systemctl is-active $u; done", check=False).split()
    if states != ["active"] * 4:
        return False, f"services: {states}"
    if domain:
        site = http_status(f"https://{domain}/")
        ops_url = f"https://{domain}/api/v1/ops"
    else:
        site = int(remote.run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7500/", check=False) or 0)
        ops_url = "http://127.0.0.1:7500/api/v1/ops"
    if site != 200:
        return False, f"the website answers {site}"
    if member_url and http_status(member_url) != 200:
        return False, f"{member_url} answers {http_status(member_url)}"
    ops = remote.run(f". /etc/airbound-controller/controller.env; curl -s -o /dev/null -w '%{{http_code}}' -H \"Authorization: Bearer $OPS_TOKEN\" {ops_url}", check=False)
    if ops.strip() != "200":
        return False, f"health endpoint answers {ops.strip()}"
    return True, "services, website, member address and health endpoint OK"


def wait_healthy(remote: Remote, domain: str | None, member_url: str | None, seconds: int = 120) -> tuple[bool, str]:
    reason = ""
    for _ in range(seconds // 10):
        ok, reason = healthy(remote, domain, member_url)
        if ok:
            return True, reason
        time.sleep(10)
    return False, reason


def deploy(remote: Remote, domain: str | None, member_url: str | None = None, simulate_failure: bool = False) -> str:
    """Returns why the new relay counts as healthy; raises DeployFailed after putting the old one back."""
    ok, reason = healthy(remote, domain, member_url)
    if not ok:
        raise DeployFailed(f"The relay isn't healthy before the deploy ({reason}); fix that first.")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    print(f"backing up to /var/backups/airbound/{stamp}.tgz")
    remote.run(f"mkdir -p /var/backups/airbound && tar czf /var/backups/airbound/{stamp}.tgz {BACKUP_PATHS} 2>/dev/null; "
               f"ls /var/backups/airbound/*.tgz | head -n -5 | xargs -r rm -f")  # keep the last five
    remote.run(safety_net())
    remote.put(str(relay_tarball()), "/root/relay.tgz")
    out = remote.run("cd /root && rm -rf relay-server airbound && tar xzf relay.tgz && cd relay-server && sh install.sh 2>&1 | tail -4", check=False, timeout=1200)
    print(out.strip())

    ok, reason = wait_healthy(remote, domain, member_url)
    if simulate_failure:
        ok, reason = False, "simulated failure (--simulate-failure)"
    if ok:
        remote.run("systemctl stop airbound-deploy-safety.timer 2>/dev/null; true")
        return reason

    print(f"ROLLING BACK: {reason}")
    remote.run(f"cd / && tar xzf /var/backups/airbound/{stamp}.tgz && systemctl daemon-reload && "
               f"systemctl restart airbound-firewall {UNITS}", check=False)
    ok, after = wait_healthy(remote, domain, member_url)
    remote.run("systemctl stop airbound-deploy-safety.timer 2>/dev/null; true")
    raise DeployFailed(f"Deploy failed ({reason}); rolled back: {'healthy again' if ok else 'STILL UNHEALTHY: ' + after}")


def main() -> None:
    target, key, domain = env("RELAY_SSH"), env("RELAY_KEY"), env("RELAY_DOMAIN")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    member_url = args[0] if args else None  # e.g. the test VM's hosted address
    with Remote(target, key) as remote:  # one SSH connection for the whole deploy
        try:
            print(f"DEPLOYED: {deploy(remote, domain, member_url, '--simulate-failure' in sys.argv)}")
        except DeployFailed as err:
            raise SystemExit(str(err)) from None


if __name__ == "__main__":
    main()
