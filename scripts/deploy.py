"""Deploy to the test VM the way users get updates: from the GitHub repository.

The add-on is installed from https://github.com/oleost/HA-AirBound, so a deploy
means: the version bump is committed and pushed, the Supervisor reloads the store
and updates the add-on, which builds it on the VM from GitHub.

Usage: python scripts/deploy.py
"""

from __future__ import annotations

import asyncio
import re
import subprocess

from ha import HA, ROOT, HAError, addon_slug, load_env


def local_version() -> str:
    match = re.search(r'^version:\s*"?([^"\n]+)"?', (ROOT / "airbound" / "config.yaml").read_text(), re.MULTILINE)
    return match.group(1)


def check_pushed() -> None:
    """GitHub must already have what we want to install."""
    status = subprocess.run(["git", "status", "--porcelain", "airbound"], cwd=ROOT, capture_output=True, text=True).stdout
    if status.strip():
        raise SystemExit("airbound/ has uncommitted changes. Commit and push them first.")
    subprocess.run(["git", "fetch", "-q"], cwd=ROOT, check=True)
    ahead = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    if ahead.strip() != "0":
        raise SystemExit("Local commits aren't on GitHub yet. Push them first.")


async def wait_for_version(slug: str, want: str, timeout: int = 1500) -> None:
    """A build can outlive the Supervisor call's timeout; the build carries on, so wait for it."""
    for _ in range(timeout // 10):
        await asyncio.sleep(10)
        try:
            async with HA() as ha:
                if (await ha.supervisor(f"/addons/{slug}/info"))["version"] == want:
                    return
        except (HAError, OSError):
            pass
    raise TimeoutError(f"the add-on didn't reach version {want}")


async def install() -> None:
    want = local_version()
    async with HA() as ha:
        slug = await addon_slug(ha)
        try:
            await ha.supervisor("/store/reload", "post", timeout=300)
        except HAError:
            print("store reload ended early; continuing")
        info = await ha.supervisor(f"/addons/{slug}/info")
        if info["version"] == want:
            print(f"already on {want}")
            return
        print(f"updating {slug} {info['version']} -> {want} (builds from GitHub on the VM)")
        try:
            await ha.supervisor(f"/store/addons/{slug}/update", "post", timeout=1800)
        except HAError as err:
            print(f"Supervisor call ended early ({err}); waiting for the build to finish")
    await wait_for_version(slug, want)
    async with HA() as ha:
        info = await ha.supervisor(f"/addons/{slug}/info")
        print("state:", info["state"], "version:", info["version"])


if __name__ == "__main__":
    load_env()
    check_pushed()
    asyncio.run(install())
