"""Shared helpers for the test round, relay staging, relay deploy and release scripts.

The test environment's private addresses live in .env (never committed):
PROXMOX_HOST, PROXMOX_KEY, TEST_VPS_VMID, TEST_VPS_IP, TEST_VPS_KEY,
DEBIAN_VPS_VMID, DEBIAN_VPS_IP,
RELAY_SSH, RELAY_KEY, RELAY_DOMAIN, the HA test VM's HA_URL/HA_WS/HA_TOKEN and
HA_VMID (scripts/confinement.py reaches it through Proxmox's guest agent),
and the beta VM's BETA_VMID/BETA_HA_TOKEN (scripts/beta.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "test-results"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

from ha import load_env  # noqa: E402

load_env()
sys.stdout.reconfigure(encoding="utf-8")  # ✓ and ✗ on a Windows console


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is missing in .env (see scripts/testenv.py)")
    return os.path.expanduser(value)


def run(cmd: list[str], check: bool = True, timeout: int = 900, quiet: bool = False) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}…: {result.stderr.strip()[-600:] or result.stdout.strip()[-600:]}")
    if not quiet and result.stdout.strip():
        print(result.stdout.strip())
    return result.stdout


def ssh(target: str, key: str, command: str, check: bool = True, timeout: int = 900) -> str:
    return run(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", target, command], check=check, timeout=timeout, quiet=True)


def scp(key: str, source: str, target: str) -> None:
    run(["scp", "-q", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", source, target])


class Remote:
    """One SSH connection for a whole job, for commands and file uploads.

    Each ssh or scp call opens a new connection, and a burst of short SSH
    connections to one address looks like a scan to an intrusion prevention
    system on the way (the maintainer's UniFi gateway blocked airbound.dev for
    that mid-deploy). Only hosts already in ~/.ssh/known_hosts are accepted.
    """

    def __init__(self, target: str, key: str) -> None:
        import paramiko

        user, _, host = target.rpartition("@")
        self._client = paramiko.SSHClient()
        self._client.load_host_keys(os.path.expanduser("~/.ssh/known_hosts"))
        self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._client.connect(host, username=user or "root", key_filename=key, look_for_keys=False, allow_agent=False, timeout=10)
        self._client.get_transport().set_keepalive(30)

    def __enter__(self) -> "Remote":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def run(self, command: str, check: bool = True, timeout: int = 900) -> str:
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out, err = stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")
        if check and stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"{command[:60]}…: {err.strip()[-600:] or out.strip()[-600:]}")
        return out

    def put(self, source: str, target: str) -> None:
        with self._client.open_sftp() as sftp:
            sftp.put(source, target)


def head_commit() -> str:
    return run(["git", "rev-parse", "HEAD"], quiet=True).strip()


def uncommitted() -> str:
    """Changed or new files git would see (test-results/ and .env are ignored)."""
    return run(["git", "status", "--porcelain"], quiet=True).strip()


_run_commit = ""


def begin_run() -> str:
    """Start a run whose result belongs to one commit: the tested code is exactly that commit.

    The test VMs install next from GitHub and the relay is packed from this
    folder, so both must match the commit: nothing uncommitted, and pushed.
    """
    global _run_commit
    if uncommitted():
        raise SystemExit(f"Uncommitted changes; commit and push them first, so the result belongs to a commit:\n{uncommitted()}")
    run(["git", "fetch", "-q", "origin"], quiet=True)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], quiet=True).strip()
    if run(["git", "rev-parse", f"origin/{branch}"], check=False, quiet=True).strip() != head_commit():
        raise SystemExit(f"HEAD isn't the same as origin/{branch}; push (or pull) first, since the test VMs install from GitHub.")
    _run_commit = head_commit()
    return _run_commit


def save_result(kind: str, results: dict) -> Path:
    """Saved under the commit the run started on. If HEAD or the files changed during the
    run, the tested code is uncertain, so the result fails whatever the tests said."""
    if not _run_commit:
        raise RuntimeError("call begin_run() before the tests start")
    moved = []
    if head_commit() != _run_commit:
        moved.append(f"HEAD moved to {head_commit()[:12]} during the run")
    if uncommitted():
        moved.append("files changed during the run")
    if moved:
        results = {**results, "provenance": {"ok": False, "detail": "; ".join(moved) + ". Run again without committing meanwhile."}}
        print(f"✗ provenance: {results['provenance']['detail']}")
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{kind}-{_run_commit[:12]}.json"
    path.write_text(json.dumps({"commit": _run_commit, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}, indent=2))
    return path


def relay_tarball() -> Path:
    """relay-server/ plus the shared hardening script, as the installer expects them."""
    out = RESULTS / "relay.tgz"
    RESULTS.mkdir(exist_ok=True)
    run(["tar", "czf", str(out), "--exclude=__pycache__", "relay-server", "airbound/airbound/vps_harden.sh"], quiet=True)
    return out


# ---------- the test VPS on Proxmox ----------

class TestVPS:
    """VM on Proxmox with a snapshot "fresh". Always stopped again afterwards.

    Ubuntu by default, the other system people run their own relay on. debian=True
    is set up like airbound.dev: Debian 13, ifupdown, no netplan or networkd, 1 GB.
    """

    def __init__(self, debian: bool = False) -> None:
        self.host = f"root@{env('PROXMOX_HOST')}"
        self.host_key = env("PROXMOX_KEY")
        self.vmid = env("DEBIAN_VPS_VMID" if debian else "TEST_VPS_VMID")
        self.ip = env("DEBIAN_VPS_IP" if debian else "TEST_VPS_IP")
        self.key = env("TEST_VPS_KEY")
        self.target = f"{'root' if debian else 'ubuntu'}@{self.ip}"

    def __enter__(self) -> "TestVPS":
        ssh(self.host, self.host_key, f"qm rollback {self.vmid} fresh && qm start {self.vmid}")
        run(["ssh-keygen", "-R", self.ip], check=False, quiet=True)
        for _ in range(60):
            if "ready" in ssh(self.target, self.key, "cloud-init status --wait >/dev/null 2>&1; echo ready", check=False, timeout=120):
                return self
            time.sleep(5)
        raise RuntimeError("the test VPS didn't come up")

    def sh(self, command: str, check: bool = True, timeout: int = 900) -> str:
        return ssh(self.target, self.key, command, check=check, timeout=timeout)

    def reboot(self) -> str:
        """Restart the server and wait for the boot to finish; returns systemd's final state."""
        self.sh("sudo systemctl reboot", check=False, timeout=30)
        time.sleep(25)
        state = "down"
        for _ in range(40):
            try:
                state = self.sh("systemctl is-system-running 2>&1", check=False, timeout=30).strip()
            except Exception:  # still restarting: ssh refuses or times out
                state = "down"
            if state in ("running", "degraded"):
                break
            time.sleep(10)
        return state

    def boot_problem(self) -> str:
        """After reboot(): what is wrong with this boot, or "" when nothing is."""
        cycles = self.sh("sudo journalctl -b --no-pager | grep -c 'ordering cycle'", check=False).strip()
        if cycles != "0":
            return f"{cycles} ordering cycles"
        if self.sh("sudo nft list table inet airbound_firewall 2>/dev/null | grep -c 'policy drop'", check=False).strip() != "1":
            return "the firewall is off"
        return ""

    def put(self, source: Path, name: str) -> None:
        scp(self.key, str(source), f"{self.target}:{name}")

    def __exit__(self, *_exc) -> None:
        ssh(self.host, self.host_key, f"qm stop {self.vmid}", check=False)
