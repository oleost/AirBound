"""GitHub Sponsors: log in with GitHub, check a sponsorship, and keep the sponsor list in sync.

GitHub accounts are stored as "gh:<GitHub user id>", next to Patreon's plain ids.
Access needs an active monthly sponsorship of at least `github_min_cents`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout

from .config import Config
from .db import Database

_LOGGER = logging.getLogger(__name__)

AUTHORIZE = "https://github.com/login/oauth/authorize"
TOKEN = "https://github.com/login/oauth/access_token"
GRAPHQL = "https://api.github.com/graphql"
USER_AGENT = "AirBound-relay/1.0 (+https://github.com/oleost/AirBound)"
TIMEOUT = ClientTimeout(total=30)
PREFIX = "gh:"

PERSON_QUERY = """
query($sponsorable: String!) {
  viewer { databaseId login }
  user(login: $sponsorable) {
    sponsorshipForViewerAsSponsor(activeOnly: true) { isOneTimePayment tier { monthlyPriceInCents isOneTime } }
  }
}"""

SPONSORS_QUERY = """
query($sponsorable: String!, $after: String) {
  user(login: $sponsorable) {
    sponsorshipsAsMaintainer(first: 100, after: $after, activeOnly: true, includePrivate: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        isOneTimePayment
        tier { monthlyPriceInCents isOneTime }
        sponsorEntity { ... on User { databaseId } ... on Organization { databaseId } }
      }
    }
  }
}"""


class GitHubError(Exception):
    pass


def account_id(github_user_id: int | str) -> str:
    return f"{PREFIX}{github_user_id}"


def is_github(account: str) -> bool:
    return account.startswith(PREFIX)


def sponsorship_counts(sponsorship: dict | None, min_cents: int) -> bool:
    """A monthly sponsorship at or above the minimum gives access; one-time payments don't."""
    if not sponsorship or sponsorship.get("isOneTimePayment"):
        return False
    tier = sponsorship.get("tier") or {}
    return not tier.get("isOneTime") and int(tier.get("monthlyPriceInCents") or 0) >= min_cents


def authorize_url(config: Config, state: str) -> str:
    return AUTHORIZE + "?" + urlencode(
        {"client_id": config.github_client_id, "redirect_uri": config.github_redirect_uri, "scope": "read:user", "state": state}
    )


def valid_signature(secret: str, body: bytes, signature: str) -> bool:
    """GitHub signs webhooks with HMAC-SHA256: X-Hub-Signature-256: sha256=<hex>."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


class GitHub:
    def __init__(self, config: Config, db: Database, session: ClientSession) -> None:
        self._config = config
        self._db = db
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(self._config.github_client_id and self._config.github_client_secret and self._config.github_sponsorable)

    async def _graphql(self, token: str, query: str, variables: dict) -> dict:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        async with self._session.post(GRAPHQL, json={"query": query, "variables": variables}, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status >= 400:
                raise GitHubError(f"GitHub answered {resp.status}: {(await resp.text())[:200]}")
            body = await resp.json(content_type=None)
        if body.get("errors"):
            raise GitHubError("; ".join(e.get("message", "") for e in body["errors"])[:300])
        return body.get("data") or {}

    # Logging in a person

    async def exchange_code(self, code: str) -> str:
        data = {
            "client_id": self._config.github_client_id,
            "client_secret": self._config.github_client_secret,
            "code": code,
            "redirect_uri": self._config.github_redirect_uri,
        }
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        async with self._session.post(TOKEN, data=data, headers=headers, timeout=TIMEOUT) as resp:
            body = await resp.json(content_type=None)
        if "access_token" not in body:
            raise GitHubError("GitHub didn't accept the login. Please try again.")
        return body["access_token"]

    async def check_person(self, access_token: str) -> tuple[str, bool]:
        """(account id, entitled) for the person who just logged in."""
        data = await self._graphql(access_token, PERSON_QUERY, {"sponsorable": self._config.github_sponsorable})
        viewer = data.get("viewer") or {}
        if not viewer.get("databaseId"):
            raise GitHubError("GitHub didn't say who logged in")
        sponsorship = (data.get("user") or {}).get("sponsorshipForViewerAsSponsor")
        return account_id(viewer["databaseId"]), sponsorship_counts(sponsorship, self._config.github_min_cents)

    # The maintainer's token, for the sponsor list

    async def sync_sponsors(self) -> int:
        """Update every GitHub account from the sponsor list. Returns how many have access."""
        if not (self.enabled and self._config.github_token):
            return 0
        entitled: dict[str, bool] = {}
        after = None
        while True:
            data = await self._graphql(self._config.github_token, SPONSORS_QUERY, {"sponsorable": self._config.github_sponsorable, "after": after})
            page = ((data.get("user") or {}).get("sponsorshipsAsMaintainer")) or {}
            for node in page.get("nodes") or []:
                sponsor = (node.get("sponsorEntity") or {}).get("databaseId")
                if sponsor:
                    key = account_id(sponsor)
                    entitled[key] = entitled.get(key, False) or sponsorship_counts(node, self._config.github_min_cents)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        for account in {a for a in self._db.account_ids() if is_github(a)} | set(entitled):
            self._db.set_entitled(account, entitled.get(account, False))
        return sum(entitled.values())
