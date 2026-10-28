"""Deploy to the test VM the way users get updates: from the GitHub repository.

The add-on is installed from https://github.com/oleost/AirBound, so a deploy
means: the version bump is committed and pushed, GitHub Actions builds the images,
then the Supervisor reloads the store and updates the add-on, pulling the image.

Usage: python scripts/deploy.py
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import urllib.request

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
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    ahead = subprocess.run(["git", "rev-list", "--count", f"origin/{branch}..HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    if ahead.strip() != "0":
        raise SystemExit("Local commits aren't on GitHub yet. Push them first.")


IMAGE = "oleost/{arch}-addon-airbound"
ARCHES = ("amd64", "aarch64")


def image_published(arch: str, version: str) -> bool:
    """Is ghcr.io/<IMAGE>:<version> there for anyone to pull?"""
    repo = IMAGE.format(arch=arch)
    try:
        with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{repo}:pull", timeout=10) as resp:
            token = json.load(resp)["token"]
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{repo}/manifests/{version}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status == 200
    except OSError:
        return False


def wait_for_images(version: str, timeout: int = 1800) -> None:
    """The Supervisor can only update once GitHub Actions has published the images."""
    deadline = time.monotonic() + timeout
    waiting = [a for a in ARCHES if not image_published(a, version)]
    if waiting:
        print(f"waiting for the {version} images ({', '.join(waiting)}) from GitHub Actions")
    while waiting:
        if time.monotonic() > deadline:
            raise SystemExit(f"the {version} images weren't published within {timeout // 60} minutes; check GitHub Actions")
        time.sleep(20)
        waiting = [a for a in waiting if not image_published(a, version)]


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
        print(f"updating {slug} {info['version']} -> {want} (pulls the image)")
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
    wait_for_images(local_version())
    asyncio.run(install())
