"""Which machine AirBound runs on, so that settings restored from a backup are noticed.

The machine key is a random value in /data that backups leave out
(backup_exclude in config.yaml). Home Assistant empties the folder before it
restores a backup, so after any restore, on this machine or another, the
settings are back but the key is new. AirBound then keeps remote access off
until the person confirms in the panel. The hosted relay only accepts the
machine that first used a device key, so a copied backup alone can't take over
an airbound.dev address: moving it needs a Patreon login as well.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .settings import Store

MACHINE_FILE = Path("/data/machine_key")


def key(path: Path = MACHINE_FILE) -> str:
    """This machine's key, made the first time it's needed."""
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    return path.read_text(encoding="utf-8").strip()


def check(store: Store, path: Path = MACHINE_FILE) -> None:
    """At startup: mark settings that came back from a backup.

    Settings from before AirBound had a machine key (machine_known unset) can't
    be told apart from a restore, so they get one without a question.
    """
    restored = not path.exists()
    key(path)
    settings = store.settings
    if restored and settings.machine_known and (settings.mode or settings.live):
        store.update(restored=True)
    if not settings.machine_known:
        store.update(machine_known=True)
