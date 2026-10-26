"""Deploy the add-on to the test VM as a local add-on, then (re)build and start it.

Usage: python scripts/deploy.py
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import tarfile

from ha import HA, ROOT, HAError, load_env

SLUG = "local_airbound"
SSH_KEY = os.path.expanduser("~/.ssh/airbound_testvm")
REMOTE_DIR = "/local_apps/airbound"


def upload() -> None:
    """Copy airbound/ to the VM's local add-on folder over SSH."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(
            ROOT / "airbound",
            arcname="airbound",
            filter=lambda t: None if "__pycache__" in t.name else t,
        )
    host = os.environ.get("HA_SSH", "root@" + os.environ["HA_URL"].split("//")[1].split(":")[0])
    subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", host,
         f"rm -rf {REMOTE_DIR} && mkdir -p /local_apps && tar -xzf - -C /local_apps"],
        input=buf.getvalue(),
        check=True,
    )
    print("uploaded")


def local_version() -> str:
    match = re.search(r'^version:\s*"?([^"\n]+)"?', (ROOT / "airbound" / "config.yaml").read_text(), re.MULTILINE)
    return match.group(1)


async def wait_for_version(want: str, timeout: int = 1500) -> None:
    """Long builds can outlive the Supervisor call's own timeout; the build carries on, so wait for it."""
    for _ in range(timeout // 10):
        await asyncio.sleep(10)
        try:
            async with HA() as ha:
                info = await ha.supervisor(f"/addons/{SLUG}/info")
            if info["version"] == want:
                return
        except (HAError, OSError):
            pass
    raise TimeoutError(f"the add-on didn't reach version {want}")


async def install() -> None:
    want = local_version()
    async with HA() as ha:
        try:
            # Reloading fetches every add-on repository online and can outlast the call.
            await ha.supervisor("/store/reload", "post", timeout=300)
        except HAError:
            print("store reload ended early; continuing")
        installed = {a["slug"]: a for a in (await ha.supervisor("/addons"))["addons"]}
        try:
            if SLUG in installed and installed[SLUG]["update_available"]:
                print(f"updating to {want}")
                await ha.supervisor(f"/store/addons/{SLUG}/update", "post", timeout=1800)
            elif SLUG in installed:
                print("rebuilding")
                await ha.supervisor(f"/addons/{SLUG}/rebuild", "post", timeout=1800)
            else:
                print("installing (this builds Caddy, first time takes a few minutes)")
                await ha.supervisor(f"/store/addons/{SLUG}/install", "post", timeout=1800)
        except HAError as err:
            print(f"Supervisor call ended early ({err}); waiting for the build to finish")
            await wait_for_version(want)
    async with HA() as ha:
        try:
            await ha.supervisor(f"/addons/{SLUG}/restart", "post", timeout=120)
        except HAError:
            await ha.supervisor(f"/addons/{SLUG}/start", "post", timeout=120)
        info = await ha.supervisor(f"/addons/{SLUG}/info")
        print("state:", info["state"], "version:", info["version"])


if __name__ == "__main__":
    load_env()
    upload()
    asyncio.run(install())
