"""A backup of the test VM's AirBound, copied to another machine: it must not get the address.

    python scripts/copied_backup.py

The test VM is the original, on the hosted relay. Its AirBound backup is
restored on the beta VM (another machine), which must stop on the restored
page and drop the key when confirmed, while the original keeps answering.
Then the copied key is tried the way an attacker with the backup would, with a
real frpc on the test VPS: airbound.dev must refuse it, and the original's
panel must warn about it. Last, the maintainer links the test VM again with
Patreon (the link opens in the browser), which gives it a new key; the address
must keep working throughout. Both VMs and the VPS end up as they were.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time

import aiohttp

from testenv import ROOT, TestVPS, env  # loads .env

from e2e import Panel, clear_own_ban, ensure_hosted, status_code  # noqa: E402
from ha import HA, addon_slug  # noqa: E402

CA = ROOT / "airbound" / "airbound" / "hosted_relay_ca.pem"
INSTALLER = (ROOT / "relay-server" / "install.sh").read_text(encoding="utf-8")
FRP_RELEASE = re.search(r'^FRP_RELEASE="([^"]+)"', INSTALLER, re.M).group(1)
FRP_VERSION = re.search(r'^FRP_VERSION="([^"]+)"', INSTALLER, re.M).group(1)
FRP_SHA = re.search(r"x86_64 \| amd64\)\n.*\n\s*FRP_SHA=(\w+)", INSTALLER).group(1)
LOGIN_TIMEOUT = 900


def step(text: str) -> None:
    print(f"✓ {text}", flush=True)


def answers(url: str) -> bool:
    try:
        return status_code(url) == 200
    except OSError:
        return False


async def open_panel() -> tuple[aiohttp.ClientSession, Panel]:
    """The panel of whichever VM HA_URL, HA_WS and HA_TOKEN point to now."""
    async with HA() as ha:
        slug = await addon_slug(ha)
        info = await ha.supervisor(f"/addons/{slug}/info")
        me = await ha.call({"type": "auth/current_user"})
        session = await ha.supervisor("/ingress/session", "post", {"user_id": me["id"]})
    client = aiohttp.ClientSession(cookies={"ingress_session": session["session"]})
    return client, Panel(client, os.environ["HA_URL"].rstrip("/") + info["ingress_url"])


async def panel_call(method: str, path: str, body: dict | None = None) -> dict:
    session, p = await open_panel()
    async with session:
        return await p.call(method, path, body)


async def state_after_restart() -> dict:
    for _ in range(100):
        try:
            return await panel_call("GET", "api/state")
        except Exception:  # still starting
            await asyncio.sleep(3)
    raise RuntimeError("the panel didn't come back")


async def take_backup(name: str) -> tuple[str, str, bytes]:
    """(backup slug, add-on slug, the backup file) from the current VM."""
    async with HA() as ha:
        slug = await addon_slug(ha)
        backup = (await ha.supervisor("/backups/new/partial", "post", {"name": name, "addons": [slug]}, timeout=900))["slug"]
    headers = {"Authorization": f"Bearer {os.environ['HA_TOKEN']}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{os.environ['HA_URL'].rstrip('/')}/api/hassio/backups/{backup}/download") as resp:
            if resp.status != 200:
                raise RuntimeError(f"backup download answered {resp.status}")
            return backup, slug, await resp.read()


def addon_files(backup_file: bytes, slug: str) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(backup_file)) as outer:
        member = next(m for m in outer.getmembers() if m.name.endswith(f"{slug}.tar.gz"))
        inner = outer.extractfile(member).read()
    with tarfile.open(fileobj=io.BytesIO(inner)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


async def upload_backup(backup_file: bytes) -> str:
    headers = {"Authorization": f"Bearer {os.environ['HA_TOKEN']}"}
    form = aiohttp.FormData()
    form.add_field("file", backup_file, filename="airbound-copy.tar", content_type="application/x-tar")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(f"{os.environ['HA_URL'].rstrip('/')}/api/hassio/backups/new/upload", data=form) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"backup upload answered {resp.status}: {data}")
            return data["data"]["slug"]


async def restore(backup: str, slug: str) -> None:
    async with HA() as ha:
        await ha.supervisor(f"/backups/{backup}/restore/partial", "post", {"addons": [slug]}, timeout=900)


async def delete_backup(backup: str) -> None:
    async with HA() as ha:
        await ha.supervisor(f"/backups/{backup}", "delete")


def use_vm(urls: dict[str, str]) -> None:
    os.environ.update(urls)


async def main() -> None:
    test_vm = {k: os.environ[k] for k in ("HA_URL", "HA_WS", "HA_TOKEN")}

    # 1. The original: the test VM on the hosted relay, and a backup of its AirBound.
    session, p = await open_panel()
    async with session:
        url = await ensure_hosted(p)
        await clear_own_ban(p)
    backup, slug, copy = await take_backup("AirBound copied-backup test")
    await delete_backup(backup)
    files = addon_files(copy, slug)
    leaked = [n for n in files if n.endswith(("machine_key", "frpc.toml"))]
    if leaked:
        raise RuntimeError(f"the backup holds {leaked}")
    settings = json.loads(next(v for n, v in files.items() if n.endswith("data/settings.json")))
    device, key, fqdn = settings["hosted_device"], settings["hosted_key"], f"{settings['hostname']}.{settings['domain']}"
    step(f"backup of the original ({fqdn}) holds its device key, but no machine key and no frpc.toml")

    # 2. The copy: the backup restored on the beta VM, another machine.
    from beta import BetaVM  # sets HA_TOKEN to the beta VM's on import

    use_vm({"HA_TOKEN": env("BETA_HA_TOKEN")})
    with BetaVM():
        await asyncio.sleep(30)  # let the Supervisor and the add-on settle after booting
        own_backup, beta_slug, _ = await take_backup("AirBound before the copied-backup test")
        try:
            copied = await upload_backup(copy)
            await restore(copied, slug)
            state = await state_after_restart()
            if not (state["settings"]["restored"] and state["landing"] == "restored"):
                raise RuntimeError(f"the copy isn't marked as restored: landing {state['landing']}")
            await asyncio.sleep(15)
            if not answers(url):
                raise RuntimeError(f"{url} stopped answering after the copy came up")
            step("the copy on the beta VM stops on the restored page, and the original keeps answering")
            state = await panel_call("POST", "api/restored", {})
            if state["settings"]["has_hosted_key"] or state["landing"] != "hosted":
                raise RuntimeError("confirming on the copy kept the copied key")
            step("confirming on the copy drops the copied key and asks for a Patreon login")
            await delete_backup(copied)
        finally:
            await restore(own_backup, beta_slug)
            state = await state_after_restart()
            if state["settings"].get("restored"):
                await panel_call("POST", "api/restored", {})
            await delete_backup(own_backup)
            step("the beta VM has its own AirBound back")
    use_vm(test_vm)

    # 3. The copied key the way an attacker would use it: a real frpc on another machine.
    with TestVPS() as vps:
        vps.sh(f"curl -fsSL -o frp.tgz https://github.com/oleost/AirBound/releases/download/{FRP_RELEASE}/frp_{FRP_VERSION}_linux_amd64.tar.gz"
               f" && echo '{FRP_SHA}  frp.tgz' | sha256sum -c - >/dev/null && tar xzf frp.tgz")
        vps.put(CA, "ca.pem")
        answers_seen = []
        for machine in ("copied-backup", ""):
            lines = [f'serverAddr = "relay.airbound.dev"', "serverPort = 7000", f'user = "{device}"', f'metadatas.key = "{key}"',
                     'auth.method = "token"', 'auth.token = "airbound-hosted-relay"', "transport.tls.enable = true",
                     'transport.tls.trustedCaFile = "ca.pem"', 'transport.tls.serverName = "airbound-hosted-relay"', "loginFailExit = true",
                     "[[proxies]]", 'name = "airbound"', 'type = "https"', f'customDomains = ["{fqdn}"]', "localPort = 1"]
            if machine:
                lines.insert(4, f'metadatas.machine = "{machine}"')
            config = "\n".join(lines)
            vps.sh(f"cat > copy.toml <<'EOF'\n{config}\nEOF")
            answer = vps.sh(f"timeout 15 ./frp_{FRP_VERSION}_linux_amd64/frpc -c copy.toml 2>&1 | tail -2; rm -f copy.toml", check=False)
            answers_seen.append(answer)
            if "other machine" not in answer:
                raise RuntimeError(f"airbound.dev didn't refuse the copied key ({machine or 'no machine'}): {answer.strip()[-300:]}")
    if not answers(url):
        raise RuntimeError(f"{url} stopped answering after the copied key was tried")
    step("airbound.dev refuses the copied key from another machine, with or without a made-up machine; the original keeps answering")

    # 4. The original warns, and a new link replaces the key while it stays live.
    async with HA() as ha:  # a restart clears AirBound's 10-minute membership cache
        await ha.supervisor(f"/addons/{slug}/restart", "post", timeout=300)
    await state_after_restart()
    warning = None
    for _ in range(30):
        try:
            checks = (await panel_call("GET", "api/status"))["checks"]
            warning = next((c for c in checks if c["title"] == "airbound.dev key" and not c["ok"]), None)
        except Exception:
            pass
        if warning:
            break
        await asyncio.sleep(5)
    if not warning:
        raise RuntimeError("the original's panel shows no warning about the copied key")
    step(f"the original warns: {warning['detail'][:90]}…")

    session, p = await open_panel()
    async with session:
        link = await p.call("POST", "api/hosted/start", {})
        print(f"\nLOGIN NEEDED: open {link['link_url']}\nCODE: {link['code']}\n", flush=True)
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "start", "", link["link_url"]], check=False)
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while time.monotonic() < deadline:
            hosted = await p.call("GET", "api/hosted")
            if hosted["state"] == "linked" and not hosted.get("code"):
                break
            if hosted["state"] == "expired":
                raise RuntimeError("the link request expired before the login")
            await asyncio.sleep(3)
        else:
            raise RuntimeError("no Patreon login within 15 minutes")
        if hosted["fqdn"] != fqdn:
            raise RuntimeError(f"a new address after linking again: {hosted['fqdn']}")
        await clear_own_ban(p)
        for _ in range(18):
            checks = (await p.call("GET", "api/status"))["checks"]
            if all(c["ok"] for c in checks) and answers(url):
                break
            await asyncio.sleep(10)
        else:
            raise RuntimeError(f"not green after linking again: {[(c['title'], c['detail'][:80]) for c in checks if not c['ok']]}")
    step(f"linked again while live: new key, same address, {url} answers and all {len(checks)} health checks are green")
    print("\nPASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as err:
        print(f"\n✗ {err}\nFAILED", flush=True)
        sys.exit(1)
