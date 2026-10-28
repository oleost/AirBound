"""AirBound on Home Assistant's beta channel, before the release reaches users.

    python scripts/beta.py

Starts the beta VM on Proxmox, moves it to the newest beta of the Supervisor,
Home Assistant and HA OS, installs or updates AirBound from the next branch,
does the wizard's Home Assistant settings step, and runs the integration
tests (MFA enforcement included). The VM is always
stopped again. HA's betas come out about a week before each monthly release;
the weekly maintenance report says when one is out.

The beta VM has no hosted address, so the tests put AirBound live with a
self-signed certificate, and every integration test runs. A failure here
doesn't block a normal release, but it must be fixed before HA's release day.
The result is written to test-results/beta-<commit>.json.

.env: BETA_VMID and BETA_HA_TOKEN (plus PROXMOX_HOST and PROXMOX_KEY, shared
with the relay test VPS). The VM's address comes from DHCP, so it is read from
the guest agent each time.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

import aiohttp

from testenv import ROOT, begin_run, env, run, save_result, ssh

# Everything that follows (ha.py, deploy.py, the integration tests) talks to the beta VM.
os.environ["HA_TOKEN"] = env("BETA_HA_TOKEN")

from ha import HA, HAError  # noqa: E402

REPOSITORY = "https://github.com/oleost/AirBound#next"


class BetaVM:
    """The HA OS VM on the beta channel. Stopped except while testing."""

    def __init__(self) -> None:
        self.host = f"root@{env('PROXMOX_HOST')}"
        self.key = env("PROXMOX_KEY")
        self.vmid = env("BETA_VMID")

    def __enter__(self) -> "BetaVM":
        ssh(self.host, self.key, f"qm start {self.vmid}", check=False)  # already running is fine
        ip = self.address()
        os.environ["HA_URL"], os.environ["HA_WS"] = f"http://{ip}", f"ws://{ip}/api/websocket"
        wait_for_core()
        return self

    def address(self) -> str:
        for _ in range(60):
            out = ssh(self.host, self.key, f"qm guest cmd {self.vmid} network-get-interfaces", check=False)
            try:
                for nic in json.loads(out):
                    for a in nic.get("ip-addresses", []):
                        if a["ip-address-type"] == "ipv4" and not a["ip-address"].startswith(("127.", "172.")):
                            return a["ip-address"]
            except ValueError:
                pass
            time.sleep(10)
        raise RuntimeError("the beta VM got no address")

    def __exit__(self, *_exc) -> None:
        # A clean shutdown through the guest agent; a hard stop if it hangs.
        ssh(self.host, self.key, f"qm shutdown {self.vmid} --timeout 180 || qm stop {self.vmid}", check=False, timeout=300)


def wait_for_core(timeout: int = 1200) -> None:
    """HA answers 401 on /api/ once Core is up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(os.environ["HA_URL"].rstrip("/") + "/api/", timeout=5)
        except urllib.error.HTTPError as err:
            if err.code == 401:
                return
        except OSError:
            pass
        time.sleep(10)
    raise RuntimeError("Home Assistant on the beta VM didn't come up")


async def supervisor(endpoint: str, method: str = "get", data: dict | None = None, timeout: int = 60):
    for _ in range(30):
        try:
            async with HA() as ha:
                return await ha.supervisor(endpoint, method, data, timeout)
        except HAError as err:
            if "unknown_command" not in str(err):
                raise
            await asyncio.sleep(10)  # Core is up, but its Supervisor integration is still loading
    raise RuntimeError("Home Assistant's Supervisor integration didn't load")


async def wait_for_supervisor(timeout: int = 900) -> None:
    """After a reboot Core can answer before the Supervisor leaves "startup", and it refuses updates until then."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await supervisor("/info"))["state"] == "running":
                return
        except Exception:  # the connection is down while it restarts
            pass
        await asyncio.sleep(10)
    raise RuntimeError("the Supervisor on the beta VM didn't reach the running state")


async def update(part: str) -> str:
    """Update /supervisor, /core or /os to the newest version on the channel; returns the version."""
    await wait_for_supervisor()
    info = await supervisor(f"/{part}/info")
    if not info.get("update_available"):
        return info["version"]
    want = info["version_latest"]
    print(f"{part}: {info['version']} -> {want}")
    try:
        await supervisor(f"/{part}/update", "post", {} if part != "core" else {"backup": False}, timeout=1800)
    except HAError as err:
        if "reboot" not in str(err):
            raise
        # An OS update that is downloaded but not active yet: the Supervisor waits for a reboot.
        await supervisor("/host/reboot", "post", timeout=60)
    except OSError:
        pass  # Core and OS restart under us; the update carries on
    for _ in range(180):
        await asyncio.sleep(10)
        try:
            if (await supervisor(f"/{part}/info"))["version"] == want:
                return want
        except Exception:  # the connection is down while it restarts
            continue
    raise RuntimeError(f"{part} didn't reach {want}")


async def to_beta() -> dict:
    if (await supervisor("/supervisor/info"))["channel"] != "beta":
        await supervisor("/supervisor/options", "post", {"channel": "beta"})
        await supervisor("/supervisor/reload", "post", timeout=300)
    versions = {"supervisor": await update("supervisor")}
    wait_for_core()
    versions["os"] = await update("os")
    wait_for_core()
    versions["core"] = await update("core")
    wait_for_core()
    return versions


async def ensure_installed() -> None:
    """A fresh beta VM gets the repository and the add-on; afterwards deploy.py keeps it current."""
    repositories = (await supervisor("/store"))["repositories"]
    ours = {r["slug"] for r in repositories if r.get("source") == REPOSITORY}
    addons = (await supervisor("/addons"))["addons"]
    for addon in addons:
        if addon["name"] == "AirBound" and addon["repository"] not in ours:
            # Installed from another repository URL (the one before the rename): start over.
            print(f"removing {addon['slug']} from repository {addon['repository']}")
            await supervisor(f"/addons/{addon['slug']}/uninstall", "post", timeout=600)
            await supervisor(f"/store/repositories/{addon['repository']}", "delete", timeout=300)
    if any(a["name"] == "AirBound" and a["repository"] in ours for a in addons):
        return
    if not any(r.get("source") == REPOSITORY for r in repositories):
        await supervisor("/store/repositories", "post", {"repository": REPOSITORY}, timeout=300)
    store = (await supervisor("/store"))["addons"]
    slug = next(a["slug"] for a in store if a["name"] == "AirBound" and a.get("repository") != "local")
    print(f"installing {slug}")
    await supervisor(f"/store/addons/{slug}/install", "post", timeout=1800)
    await supervisor(f"/addons/{slug}/start", "post", timeout=300)


async def ha_settings() -> str:
    """The wizard's "Home Assistant settings" step, through the panel as a user would do it."""
    from conftest import ingress  # reads HA_URL, so only once the VM's address is known

    async def call(method: str, path: str, body: dict | None = None) -> dict:
        base, cookie = await ingress()
        async with aiohttp.ClientSession(cookies={"ingress_session": cookie}) as s:
            async with s.request(method, base + path, json=body) as r:
                r.raise_for_status()
                return await r.json()

    status = await call("GET", "api/ha-settings")
    if not status["changes"] and not status["pending"] and status["proxy_trusted"]:
        return "already done"
    if not status["pending"]:
        await call("POST", "api/ha-settings/apply", {})  # HA restarts to try them
    for _ in range(60):
        await asyncio.sleep(10)
        try:
            status = await call("GET", "api/ha-settings")
        except Exception:  # HA or the panel is still restarting
            continue
        if status["pending"]:
            await call("POST", "api/ha-settings/confirm", {})
        elif not status["changes"] and status["proxy_trusted"]:
            return "applied and confirmed"
    raise RuntimeError(f"the HA settings didn't settle: {status}")


def main() -> None:
    print(f"testing {begin_run()[:12]}")
    results, failed = {}, False
    with BetaVM():
        for name, step in (("beta", to_beta), ("install", ensure_installed)):
            started = time.monotonic()
            try:
                detail = asyncio.run(step())
                results[name] = {"ok": True, "detail": detail, "seconds": round(time.monotonic() - started)}
                print(f"✓ {name}: {json.dumps(detail) if detail else 'done'}")
            except Exception as err:
                results[name] = {"ok": False, "detail": str(err)}
                print(f"✗ {name}: {err}")
                failed = True
                break
        if not failed:
            for name, cmd, timeout in (
                ("deploy", [sys.executable, "scripts/deploy.py"], 2400),
                ("integration", [sys.executable, "-m", "pytest", "-q", "tests/integration"], 1800),
            ):
                started = time.monotonic()
                try:
                    out = run(cmd, timeout=timeout, quiet=True).strip().splitlines()
                    results[name] = {"ok": True, "detail": out[-1] if out else "", "seconds": round(time.monotonic() - started)}
                    print(f"✓ {name}: {results[name]['detail']}")
                except Exception as err:
                    results[name] = {"ok": False, "detail": str(err)}
                    print(f"✗ {name}: {err}")
                    failed = True
                    break
                if name == "deploy":
                    try:
                        detail = asyncio.run(ha_settings())
                        results["ha_settings"] = {"ok": True, "detail": detail}
                        print(f"✓ ha_settings: {detail}")
                    except Exception as err:
                        results["ha_settings"] = {"ok": False, "detail": str(err)}
                        print(f"✗ ha_settings: {err}")
                        failed = True
                        break
    path = save_result("beta", results)
    print(f"\n{'FAILED' if failed else 'PASSED'}; the beta VM is stopped; result in {path.relative_to(ROOT)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
