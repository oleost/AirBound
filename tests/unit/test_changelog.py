"""Home Assistant shows airbound/CHANGELOG.md when an update is offered."""

import re
from pathlib import Path

ADDON = Path(__file__).resolve().parents[2] / "airbound"


def test_this_version_says_what_changed():
    version = re.search(r'^version: "([^"]+)"', (ADDON / "config.yaml").read_text(encoding="utf-8"), re.M).group(1)
    sections = re.split(r"^## ", (ADDON / "CHANGELOG.md").read_text(encoding="utf-8"), flags=re.M)[1:]
    assert sections, "CHANGELOG.md has no versions"
    newest, _, text = sections[0].partition("\n")
    assert newest.strip() == version, f"CHANGELOG.md must start with ## {version}, the version in config.yaml"
    assert re.search(r"^- \S", text, re.M), f"the entry for {version} is empty"


def test_a_released_version_is_never_changed():
    """An image is published under its version number. Once that version is released (tag v<version>),
    a change to the add-on needs a new number, or the released image would be replaced by untested code."""
    import subprocess

    import pytest

    version = re.search(r'^version: "([^"]+)"', (ADDON / "config.yaml").read_text(encoding="utf-8"), re.M).group(1)
    root = ADDON.parent
    try:
        tagged = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/v{version}"], cwd=root, capture_output=True).returncode == 0
    except OSError:
        pytest.skip("git isn't available")
    if not tagged:
        return
    changed = subprocess.run(["git", "diff", "--quiet", f"v{version}", "--", "airbound"], cwd=root).returncode != 0
    assert not changed, f"v{version} is released: bump the version in airbound/config.yaml before changing the add-on"
