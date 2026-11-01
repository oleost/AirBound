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
