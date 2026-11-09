"""Bring the test VM to the newest stable Home Assistant: Supervisor, HA OS and Core.

    python scripts/update_test_vm.py [--check]

The test VM tests what users run, so it stays on the stable channel (the beta
VM covers betas, scripts/beta.py); a VM found on another channel is moved back.
An OS update that waits for a restart gets one. --check only prints the
versions. Run it after each Home Assistant release, then scripts/e2e.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

from testenv import env

# beta.py's update helpers, pointed at the test VM instead of the beta VM.
TEST_VM = {name: env(name) for name in ("HA_TOKEN", "HA_URL", "HA_WS")}
import beta  # noqa: E402  (sets the beta VM's token on import)

os.environ.update(TEST_VM)


async def main() -> None:
    check_only = "--check" in sys.argv
    channel = (await beta.supervisor("/supervisor/info"))["channel"]
    if channel != "stable":
        print(f"channel: {channel}" + ("" if check_only else " -> stable"))
        if not check_only:
            await beta.supervisor("/supervisor/options", "post", {"channel": "stable"})
            await beta.supervisor("/supervisor/reload", "post", timeout=300)
    for part in ("supervisor", "os", "core"):
        info = await beta.supervisor(f"/{part}/info")
        print(f"{part}: {info['version']} (newest {info['version_latest']})")
    if check_only:
        return

    issues = [i["type"] for i in (await beta.supervisor("/resolution/info"))["issues"]]
    if "reboot_required" in issues:  # an OS update is staged and waits for a restart
        print("restarting for a staged OS update")
        try:
            await beta.supervisor("/host/reboot", "post", timeout=60)
        except Exception:  # the connection drops as it goes down
            pass
        await asyncio.sleep(60)
    for part in ("supervisor", "os", "core"):
        beta.wait_for_core()
        print(f"{part}: {await beta.update(part)}")
    beta.wait_for_core()


if __name__ == "__main__":
    asyncio.run(main())
