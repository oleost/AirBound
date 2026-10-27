"""The test round before a release: everything that runs against the test VM and the test VPS.

    python scripts/e2e.py            all scenarios (about 20 minutes)
    python scripts/e2e.py --skip own_relay

Scenarios, in order: unit tests, deploy main to the test VM, the hosted relay
end to end, the integration tests (including MFA enforcement), MFA-guard bypass
probes from outside, and own-relay mode on a fresh test VPS. The test VM ends
up back on the hosted relay, and the VPS is stopped. The result is written to
test-results/e2e-<commit>.json, which scripts/release.py requires.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
import time
import traceback
import urllib.request

import aiohttp
from yarl import URL

from testenv import ROOT, TestVPS, run, save_result

from conftest import LOCAL_URL, create_user, ingress  # noqa: E402
from ha import HA  # noqa: E402

TEST_NAME = ("k7f3q9x2m4p8", "airbound.test")  # own-relay test address (self-signed)
BYPASS_STEPS = ["/auth/login_flow/{id}", "//auth/login_flow/{id}", "/auth//login_flow/{id}", "/AUTH/login_flow/{id}",
                "/auth/%6Cogin_flow/{id}", "/auth/login_flow/{id}/", "/x/../auth/login_flow/{id}", "/auth/./login_flow/{id}",
                "/auth/login_flow/%2e%2e/login_flow/{id}", "/auth/login_flow/{id}?a=b", "/auth/login_flow/{id};x"]


class Panel:
    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self.session, self.base = session, base

    async def call(self, method: str, path: str, body: dict | None = None) -> dict:
        async with self.session.request(method, self.base + path, json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"{path}: {resp.status} {data}")
            return data

    async def go_live(self) -> dict:
        await self.call("POST", "api/go-live", {})
        for _ in range(150):
            progress = await self.call("GET", "api/go-live")
            if not progress["busy"]:
                return progress
            await asyncio.sleep(2)
        raise RuntimeError("go-live didn't finish")


async def panel() -> tuple[aiohttp.ClientSession, Panel]:
    base, cookie = await ingress()
    session = aiohttp.ClientSession(cookies={"ingress_session": cookie})
    return session, Panel(session, base)


def status_code(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def my_public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=10) as resp:
        return resp.read().decode().strip()


# ---------- scenarios ----------

def unit() -> str:
    run([sys.executable, "-m", "pytest", "-q", "tests/unit", "tests/relay_server", "tests/ui"], quiet=True)
    return "unit, relay and browser tests pass"


def deploy() -> str:
    run([sys.executable, "scripts/deploy.py"], timeout=2400)
    return "test VM runs the version on next"


async def ensure_hosted(p: Panel) -> str:
    settings = (await p.call("GET", "api/state"))["settings"]
    if not (settings["mode"] == "hosted" and settings["live"] and settings["tls"] == "hosted"):
        await p.call("POST", "api/connection", {"mode": "hosted"})
        await p.call("GET", "api/hosted")
        await p.go_live()
        settings = (await p.call("GET", "api/state"))["settings"]
    if not (settings["mode"] == "hosted" and settings["live"]):
        raise RuntimeError(f"couldn't get back to the hosted relay: {settings['mode']} live={settings['live']}")
    return settings["url"]


async def hosted() -> str:
    session, p = await panel()
    async with session:
        url = await ensure_hosted(p)
        for _ in range(12):  # Caddy and the tunnel may still be starting after a deploy
            checks = (await p.call("GET", "api/status"))["checks"]
            if all(c["ok"] for c in checks) and status_code(url) == 200:
                return f"{url}: all {len(checks)} health checks green, reachable from outside"
            await asyncio.sleep(10)
        bad = [c["title"] for c in checks if not c["ok"]]
        raise RuntimeError(f"health checks not green: {bad}; outside answers {status_code(url)}")


def integration() -> str:
    out = run([sys.executable, "-m", "pytest", "-q", "tests/integration"], timeout=1800, quiet=True)
    return out.strip().splitlines()[-1]


async def bypass() -> str:
    """No spelling of the login-flow path may finish a remote login without MFA."""
    session, p = await panel()
    async with session:
        url = await ensure_hosted(p)
        name, password = "e2ebyp" + secrets.token_hex(3), secrets.token_urlsafe(16)
        async with HA() as ha:
            user_id = await create_user(ha, name, password)
        bypassed = []
        try:
            async with aiohttp.ClientSession() as outside:
                client = {"client_id": url + "/", "handler": ["homeassistant", None], "redirect_uri": url + "/"}
                for step in BYPASS_STEPS:
                    async with outside.post(url + "/auth/login_flow", json=client) as r:
                        flow = (await r.json(content_type=None)).get("flow_id")
                    target = URL(url + step.replace("{id}", flow or "x"), encoded=True)
                    async with outside.post(target, json={"client_id": url + "/", "username": name, "password": password}) as r:
                        if '"create_entry"' in await r.text():
                            bypassed.append(step)
        finally:
            async with HA() as ha:
                await ha.call({"type": "config/auth/delete", "user_id": user_id})
            # The probes look like an attack, and CrowdSec rightly blocks this address for it.
            ip = my_public_ip()
            if any(b["ip"] == ip for b in (await p.call("GET", "api/bans"))["bans"]):
                await p.call("POST", "api/bans/unban", {"ip": ip})
        if bypassed:
            raise RuntimeError(f"MFA BYPASS through: {bypassed}")
        return f"{len(BYPASS_STEPS)} path spellings, none finished a login without MFA"


async def own_relay() -> str:
    """The add-on's own-relay command on a fresh VPS, a real tunnel through it, then back to hosted."""
    session, p = await panel()
    async with session, asyncio.timeout(1800):
        await ensure_hosted(p)
        with TestVPS() as vps:
            try:
                await p.call("POST", "api/connection", {"mode": "relay"})
                await p.call("POST", "api/settings", {"tls": "internal", "domain": TEST_NAME[1], "hostname": TEST_NAME[0]})
                command = (await p.call("GET", "api/relay"))["command"]
                path = ROOT / "test-results" / "relay-command.txt"
                path.write_text(command, newline="\n")
                try:
                    vps.put(path, "cmd.txt")
                finally:
                    path.unlink()
                installed = vps.sh('sh -c "$(cat cmd.txt)" 2>&1; rm -f cmd.txt')
                if "AirBound relay is running" not in installed:
                    raise RuntimeError(f"install command failed: {installed[-400:]}")
                score = vps.sh("systemd-analyze security airbound-relay --no-pager | tail -1")
                await p.call("POST", "api/relay", {"host": vps.ip})
                progress = await p.go_live()
                if not progress["live"]:
                    raise RuntimeError(f"go-live through the own relay failed: {progress['tasks']}")
                logins = vps.sh("sudo journalctl -u airbound-relay --no-pager | grep -c 'new proxy'", check=False).strip()
                if not logins or logins == "0":
                    raise RuntimeError("the relay never saw the tunnel")
            finally:
                url = await ensure_hosted(p)
        if status_code(url) != 200:
            raise RuntimeError(f"back on the hosted relay, but {url} answers {status_code(url)}")
        exposure = re.search(r"(\d+\.\d+)\s+\w+", score)  # the line ends in a smiley with a colon
        return f"fresh VPS: installed and hardened (exposure {exposure.group(1) if exposure else '?'}), tunnel up, back on {url}"


SCENARIOS = [("unit", unit), ("deploy", deploy), ("hosted", hosted), ("integration", integration), ("bypass", bypass), ("own_relay", own_relay)]


def main() -> None:
    skip = set(sys.argv[sys.argv.index("--skip") + 1:]) if "--skip" in sys.argv else set()
    results, failed = {}, False
    for name, scenario in SCENARIOS:
        if name in skip:
            results[name] = {"ok": None, "detail": "skipped"}
            print(f"- {name}: skipped")
            continue
        started = time.monotonic()
        try:
            detail = asyncio.run(scenario()) if asyncio.iscoroutinefunction(scenario) else scenario()
            results[name] = {"ok": True, "detail": detail, "seconds": round(time.monotonic() - started)}
            print(f"✓ {name}: {detail}")
        except Exception as err:  # every scenario reports, even after a failure
            failed = True
            results[name] = {"ok": False, "detail": str(err), "seconds": round(time.monotonic() - started)}
            print(f"✗ {name}: {err}")
            traceback.print_exc(limit=2)
            if name in ("unit", "deploy"):
                break  # nothing after this is meaningful
    path = save_result("e2e", results)
    print(f"\n{'FAILED' if failed else 'PASSED'}; result in {path.relative_to(ROOT)}")
    print(json.dumps({k: v["ok"] for k, v in results.items()}))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
