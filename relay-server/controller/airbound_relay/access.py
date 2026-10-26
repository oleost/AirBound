"""Who may use the relay right now."""

from __future__ import annotations

import time

from .db import Account

ACTIVE, GRACE, EXPIRED = "active", "grace", "expired"


def member_entitled(member: dict, tier_id: str) -> bool:
    """A Patreon member gets access while paying (or on the free trial) for the relay tier."""
    attributes = member.get("attributes") or {}
    if attributes.get("patron_status") != "active_patron":
        return False
    tiers = ((member.get("relationships") or {}).get("currently_entitled_tiers") or {}).get("data") or []
    return any(t.get("id") == tier_id for t in tiers)


def state(account: Account | None, patreon_id: str, free_users: frozenset[str], grace_days: int, now: float | None = None) -> str:
    if patreon_id in free_users:
        return ACTIVE
    if account is None:
        return EXPIRED
    if account.entitled:
        return ACTIVE
    if account.lapsed_at and (now or time.time()) < account.lapsed_at + grace_days * 86400:
        return GRACE
    return EXPIRED


def grace_until(account: Account | None, grace_days: int) -> float | None:
    if account is None or account.entitled or not account.lapsed_at:
        return None
    return account.lapsed_at + grace_days * 86400
