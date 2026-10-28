"""A restored backup in hosted mode, on the test VM: the address comes back only after a Patreon login.

    python scripts/hosted_restore.py

Takes a backup of AirBound on the test VM, restores it, checks that the
address stays off, confirms on the restored page, and starts a new link. Then
the maintainer logs in with Patreon (the link opens in the browser; the same
account as the test VM uses), and the script checks that the test VM got the
same address back, goes live and answers from outside. Needs a person, so it
isn't part of the test round. The VM ends up on the hosted relay as before.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from testenv import ROOT, run  # noqa: F401  (loads .env and the paths)

from e2e import clear_own_ban, ensure_hosted, panel, status_code  # noqa: E402
from ha import HA, addon_slug  # noqa: E402

LOGIN_TIMEOUT = 900  # a link request lives 15 minutes


def answers(url: str) -> bool:
    try:
        return status_code(url) == 200
    except OSError:  # nothing listens: refused or timed out
        return False


async def state_after_restart() -> dict:
    for _ in range(100):
        try:
            session, p = await panel()
            async with session:
                return await p.call("GET", "api/state")
        except Exception:  # still starting
            await asyncio.sleep(3)
    raise RuntimeError("the panel didn't come back after the restore")


async def main() -> None:
    session, p = await panel()
    async with session:
        url = await ensure_hosted(p)
        await clear_own_ban(p)
        before = (await p.call("GET", "api/state"))["settings"]["fqdn"]
    print(f"✓ live on the hosted relay: {url}")

    async with HA() as ha:
        slug = await addon_slug(ha)
        backup = (await ha.supervisor("/backups/new/partial", "post", {"name": "AirBound hosted restore test", "addons": [slug]}, timeout=900))["slug"]
        print("✓ backup taken; restoring it")
        await ha.supervisor(f"/backups/{backup}/restore/partial", "post", {"addons": [slug]}, timeout=900)
    try:
        state = await state_after_restart()
        if not (state["settings"]["restored"] and state["landing"] == "restored"):
            raise RuntimeError(f"not marked as restored: landing {state['landing']}")
        await asyncio.sleep(15)
        if answers(url):
            raise RuntimeError(f"{url} answers although the restore isn't confirmed")
        print("✓ restored: the panel asks first, and the address is off")

        session, p = await panel()
        async with session:
            state = await p.call("POST", "api/restored", {})
            if state["landing"] != "hosted" or state["settings"]["has_hosted_key"]:
                raise RuntimeError(f"after confirming: landing {state['landing']}, key kept {state['settings']['has_hosted_key']}")
            print("✓ confirmed: the old key is gone and the panel asks for a Patreon login")
            link = await p.call("POST", "api/hosted/start", {})
            print(f"\nLOGIN NEEDED: open {link['link_url']}\nCODE: {link['code']}\n", flush=True)
            if sys.platform == "win32":
                subprocess.run(["cmd", "/c", "start", "", link["link_url"]], check=False)
            deadline = time.monotonic() + LOGIN_TIMEOUT
            while time.monotonic() < deadline:
                hosted = await p.call("GET", "api/hosted")
                if hosted["state"] == "linked":
                    break
                if hosted["state"] == "expired":
                    raise RuntimeError("the link request expired before the login")
                await asyncio.sleep(3)
            else:
                raise RuntimeError("no Patreon login within 15 minutes")
            if hosted["fqdn"] != before:
                raise RuntimeError(f"a new address after linking again: {hosted['fqdn']} instead of {before}")
            print(f"✓ linked again with the same address: {before}")
            progress = await p.go_live()
            if not progress["live"]:
                raise RuntimeError(f"going live failed: {progress['tasks']}")
            await clear_own_ban(p)
            for _ in range(12):
                checks = (await p.call("GET", "api/status"))["checks"]
                if all(c["ok"] for c in checks) and answers(url):
                    print(f"✓ live again: {url} answers from outside, all {len(checks)} health checks green")
                    break
                await asyncio.sleep(10)
            else:
                raise RuntimeError(f"not green after going live: {[c['title'] for c in checks if not c['ok']]}")
    finally:
        async with HA() as ha:
            await ha.supervisor(f"/backups/{backup}", "delete")
    print("\nPASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as err:
        print(f"\n✗ {err}\nFAILED")
        sys.exit(1)
