"""Patreon: log in with Patreon, check a member's tier, and keep the member list in sync."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout

from .access import member_entitled
from .config import Config
from .db import Database

_LOGGER = logging.getLogger(__name__)

API = "https://www.patreon.com/api/oauth2/v2"
AUTHORIZE = "https://www.patreon.com/oauth2/authorize"
TOKEN = "https://www.patreon.com/api/oauth2/token"
USER_AGENT = "AirBound-relay/1.0 (+https://github.com/oleost/HA-AirBound)"
TIMEOUT = ClientTimeout(total=30)
MEMBER_FIELDS = "patron_status,is_free_trial,last_charge_status"
WEBHOOK_TRIGGERS = ["members:create", "members:update", "members:delete", "members:pledge:create", "members:pledge:update", "members:pledge:delete"]


class PatreonError(Exception):
    pass


def authorize_url(config: Config, state: str) -> str:
    return AUTHORIZE + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": "identity identity.memberships",
            "state": state,
        }
    )


def membership_in(identity: dict, campaign_id: str, tier_id: str) -> tuple[str, bool]:
    """(Patreon user id, entitled) from an identity response that includes memberships."""
    user_id = (identity.get("data") or {}).get("id", "")
    for item in identity.get("included") or []:
        if item.get("type") != "member":
            continue
        campaign = ((item.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        if campaign.get("id") == campaign_id and member_entitled(item, tier_id):
            return user_id, True
    return user_id, False


def members_entitled(pages: list[dict], tier_id: str) -> dict[str, bool]:
    """Patreon user id -> entitled, from campaign member list pages."""
    result: dict[str, bool] = {}
    for page in pages:
        for member in page.get("data") or []:
            user = ((member.get("relationships") or {}).get("user") or {}).get("data") or {}
            if user.get("id"):
                result[user["id"]] = result.get(user["id"], False) or member_entitled(member, tier_id)
    return result


def valid_signature(secret: str, body: bytes, signature: str) -> bool:
    """Patreon signs webhooks with HMAC-MD5 of the body (their choice, not ours)."""
    expected = hmac.new(secret.encode(), body, hashlib.md5).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


class Patreon:
    def __init__(self, config: Config, db: Database, session: ClientSession) -> None:
        self._config = config
        self._db = db
        self._session = session

    async def _request(self, method: str, url: str, token: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        async with self._session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs) as resp:
            if resp.status == 401:
                raise PatreonError("unauthorized")
            if resp.status >= 400:
                raise PatreonError(f"Patreon answered {resp.status}: {(await resp.text())[:200]}")
            return await resp.json(content_type=None)

    # Logging in a person

    async def exchange_code(self, code: str) -> str:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "redirect_uri": self._config.redirect_uri,
        }
        async with self._session.post(TOKEN, data=data, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in body:
                raise PatreonError("Patreon didn't accept the login. Please try again.")
            return body["access_token"]

    async def check_person(self, access_token: str) -> tuple[str, bool]:
        query = urlencode(
            {
                "include": "memberships.currently_entitled_tiers,memberships.campaign",
                "fields[member]": MEMBER_FIELDS,
            }
        )
        identity = await self._request("GET", f"{API}/identity?{query}", access_token)
        return membership_in(identity, self._config.campaign_id, self._config.tier_id)

    # The creator's token, for the member list and webhooks

    def _creator_tokens(self) -> tuple[str, str]:
        stored = self._db.get("creator_token")
        if stored:
            data = json.loads(stored)
            return data["access_token"], data["refresh_token"]
        return self._config.creator_access_token, self._config.creator_refresh_token

    async def _refresh_creator_token(self) -> str:
        _access, refresh = self._creator_tokens()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        async with self._session.post(TOKEN, data=data, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in body:
                raise PatreonError("couldn't refresh the creator token; create a new one in the Patreon portal")
        self._db.put(
            "creator_token",
            json.dumps({"access_token": body["access_token"], "refresh_token": body.get("refresh_token", refresh), "refreshed": time.time()}),
        )
        _LOGGER.info("Refreshed the Patreon creator token")
        return body["access_token"]

    async def _creator(self, method: str, url: str, **kwargs) -> dict:
        token, _refresh = self._creator_tokens()
        try:
            return await self._request(method, url, token, **kwargs)
        except PatreonError as err:
            if str(err) != "unauthorized":
                raise
        return await self._request(method, url, await self._refresh_creator_token(), **kwargs)

    async def sync_members(self) -> int:
        """Update every known account from the campaign's member list. Returns how many have access."""
        query = urlencode(
            {
                "include": "currently_entitled_tiers,user",
                "fields[member]": MEMBER_FIELDS,
                "page[count]": "500",
            }
        )
        url = f"{API}/campaigns/{self._config.campaign_id}/members?{query}"
        pages = []
        while url:
            page = await self._creator("GET", url)
            pages.append(page)
            url = ((page.get("links") or {}).get("next")) or ""
        entitled = members_entitled(pages, self._config.tier_id)
        patreon_accounts = {a for a in self._db.account_ids() if not a.startswith("gh:")}  # GitHub has its own sync
        for patreon_id in patreon_accounts | set(entitled):
            self._db.set_entitled(patreon_id, entitled.get(patreon_id, False))
        return sum(entitled.values())

    async def ensure_webhook(self) -> None:
        """Patreon tells us right away when a membership starts, changes or ends."""
        if self._db.get("webhook_secret"):
            return
        uri = f"{self._config.base_url}/patreon/webhook"
        body = {
            "data": {
                "type": "webhook",
                "attributes": {"triggers": WEBHOOK_TRIGGERS, "uri": uri},
                "relationships": {"campaign": {"data": {"type": "campaign", "id": self._config.campaign_id}}},
            }
        }
        result = await self._creator("POST", f"{API}/webhooks", json=body)
        secret = ((result.get("data") or {}).get("attributes") or {}).get("secret")
        if secret:
            self._db.put("webhook_secret", secret)
            _LOGGER.info("Created the Patreon webhook for %s", uri)
