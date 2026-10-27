"""The controller's database: Patreon accounts, devices (Home Assistants) and link requests.

Only what's needed is kept: the Patreon user id, whether the membership gives
access, per device a hash of its key, its hostname and when it was last seen,
and for fair use how much data it used each month (kept for three months). No
names, email addresses or anything about the traffic's content.
"""

from __future__ import annotations

import functools
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    patreon_id TEXT PRIMARY KEY,
    entitled INTEGER NOT NULL DEFAULT 0,
    lapsed_at REAL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    patreon_id TEXT NOT NULL REFERENCES accounts(patreon_id),
    install_id TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    last_seen REAL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    poll_hash TEXT NOT NULL,
    install_id TEXT NOT NULL,
    state TEXT NOT NULL,
    patreon_id TEXT,
    device_id TEXT,
    device_key TEXT,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic (
    month TEXT NOT NULL,
    device_id TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (month, device_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CLAIM_TTL = 900  # a link request is valid for 15 minutes
RESERVED = frozenset({"www", "relay", "api", "admin", "mail", "status", "docs", "app"})


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


WORDS_FILE = Path(__file__).with_name("words.txt")  # the same list as the add-on's


@functools.cache
def address_words() -> tuple[str, ...]:
    lines = WORDS_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def new_hostname() -> str:
    """Three different random words, such as otter-comet-maple, like the add-on suggests."""
    while True:
        name = "-".join(secrets.SystemRandom().sample(address_words(), 3))
        if name not in RESERVED:
            return name


@dataclass
class Device:
    id: str
    patreon_id: str
    install_id: str
    hostname: str
    created: float
    last_seen: float | None
    revoked: bool


@dataclass
class Account:
    patreon_id: str
    entitled: bool
    lapsed_at: float | None


class Database:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    # Accounts

    def account(self, patreon_id: str) -> Account | None:
        row = self._db.execute("SELECT * FROM accounts WHERE patreon_id = ?", (patreon_id,)).fetchone()
        return Account(row["patreon_id"], bool(row["entitled"]), row["lapsed_at"]) if row else None

    def set_entitled(self, patreon_id: str, entitled: bool, now: float | None = None) -> Account:
        """Record whether the membership gives access. The grace period starts when it stops."""
        now = now or time.time()
        current = self.account(patreon_id)
        if current is None:
            self._db.execute(
                "INSERT INTO accounts (patreon_id, entitled, lapsed_at, created, updated) VALUES (?, ?, NULL, ?, ?)",
                (patreon_id, int(entitled), now, now),
            )
            # A brand-new account that isn't entitled never had access, so no grace period.
            return Account(patreon_id, entitled, None)
        lapsed_at = None if entitled else (current.lapsed_at or (now if current.entitled else current.lapsed_at))
        self._db.execute(
            "UPDATE accounts SET entitled = ?, lapsed_at = ?, updated = ? WHERE patreon_id = ?",
            (int(entitled), lapsed_at, now, patreon_id),
        )
        return Account(patreon_id, entitled, lapsed_at)

    def account_ids(self) -> list[str]:
        return [r["patreon_id"] for r in self._db.execute("SELECT patreon_id FROM accounts")]

    # Devices

    def _device(self, row) -> Device | None:
        if row is None:
            return None
        return Device(row["id"], row["patreon_id"], row["install_id"], row["hostname"], row["created"], row["last_seen"], bool(row["revoked"]))

    def device_by_key(self, key: str) -> Device | None:
        row = self._db.execute("SELECT * FROM devices WHERE key_hash = ? AND revoked = 0", (digest(key),)).fetchone()
        return self._device(row)

    def devices_of(self, patreon_id: str) -> list[Device]:
        rows = self._db.execute(
            "SELECT * FROM devices WHERE patreon_id = ? AND revoked = 0 ORDER BY COALESCE(last_seen, created)", (patreon_id,)
        )
        return [d for d in (self._device(r) for r in rows) if d]

    def link_device(self, patreon_id: str, install_id: str, max_devices: int, now: float | None = None) -> tuple[Device, str]:
        """A device and its new key for this Home Assistant.

        Linking the same Home Assistant again keeps its address and replaces the
        key. Past the device limit, the device seen least recently is unlinked.
        """
        now = now or time.time()
        key = new_secret()
        existing = [d for d in self.devices_of(patreon_id) if d.install_id == install_id]
        if existing:
            device = existing[0]
            self._db.execute("UPDATE devices SET key_hash = ? WHERE id = ?", (digest(key), device.id))
            return device, key
        others = self.devices_of(patreon_id)
        for old in others[: max(0, len(others) - max_devices + 1)]:
            self._db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (old.id,))
        while True:
            hostname = new_hostname()
            if not self._db.execute("SELECT 1 FROM devices WHERE hostname = ?", (hostname,)).fetchone():
                break
        device = Device("d" + secrets.token_hex(8), patreon_id, install_id, hostname, now, None, False)
        self._db.execute(
            "INSERT INTO devices (id, patreon_id, install_id, key_hash, hostname, created) VALUES (?, ?, ?, ?, ?, ?)",
            (device.id, patreon_id, install_id, digest(key), hostname, now),
        )
        return device, key

    def count_devices(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM devices WHERE revoked = 0").fetchone()[0]

    def count_entitled(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM accounts WHERE entitled = 1").fetchone()[0]

    def seen(self, device_id: str, now: float | None = None) -> None:
        self._db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now or time.time(), device_id))

    # Link requests ("claims"): Home Assistant asks, the person logs in with Patreon, Home Assistant picks up the key

    def new_claim(self, install_id: str, now: float | None = None) -> tuple[str, str]:
        now = now or time.time()
        claim_id, poll = secrets.token_urlsafe(16), new_secret()
        self._db.execute(
            "INSERT INTO claims (id, poll_hash, install_id, state, created, expires) VALUES (?, ?, ?, 'pending', ?, ?)",
            (claim_id, digest(poll), install_id, now, now + CLAIM_TTL),
        )
        self._db.execute("DELETE FROM claims WHERE expires < ?", (now - 86400,))
        return claim_id, poll

    def claim(self, claim_id: str, now: float | None = None) -> sqlite3.Row | None:
        row = self._db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None or row["expires"] < (now or time.time()):
            return None
        return row

    def claim_for_poll(self, claim_id: str, poll: str, now: float | None = None) -> sqlite3.Row | None:
        row = self.claim(claim_id, now)
        if row is None or not secrets.compare_digest(row["poll_hash"], digest(poll)):
            return None
        return row

    def set_claim(self, claim_id: str, state: str, patreon_id: str | None = None, device_id: str | None = None, device_key: str | None = None) -> None:
        self._db.execute(
            "UPDATE claims SET state = ?, patreon_id = COALESCE(?, patreon_id), device_id = COALESCE(?, device_id), device_key = ? WHERE id = ?",
            (state, patreon_id, device_id, device_key, claim_id),
        )

    def take_claim_key(self, claim_id: str) -> None:
        """The key is handed out once, then only its hash remains."""
        self._db.execute("UPDATE claims SET device_key = NULL, state = 'delivered' WHERE id = ?", (claim_id,))

    def device(self, device_id: str) -> Device | None:
        return self._device(self._db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())

    # Monthly traffic, for fair use. "server" is what the whole server sent.

    def traffic(self, month: str, device_id: str) -> int:
        row = self._db.execute("SELECT bytes FROM traffic WHERE month = ? AND device_id = ?", (month, device_id)).fetchone()
        return row["bytes"] if row else 0

    def add_traffic(self, month: str, device_id: str, amount: int) -> None:
        self._db.execute(
            "INSERT INTO traffic (month, device_id, bytes) VALUES (?, ?, ?) "
            "ON CONFLICT(month, device_id) DO UPDATE SET bytes = bytes + excluded.bytes",
            (month, device_id, amount),
        )

    def server_traffic(self, month: str) -> int:
        return self.traffic(month, "server")

    def add_server_traffic(self, month: str, amount: int) -> None:
        self.add_traffic(month, "server", amount)

    def forget_traffic_before(self, month: str) -> None:
        self._db.execute("DELETE FROM traffic WHERE month < ?", (month,))

    # Small persistent values (the creator token after refreshes, the webhook secret)

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
