"""GitHub App authentication against the real API.

test_auth.py covers the parts that can be faked - PEM loading, JWT claims,
cache expiry arithmetic. What it cannot cover is whether GitHub actually
accepts any of it, and that is the half that fails in practice: a JWT signed
with the wrong key, an app id that does not match, a clock skewed past the
10-minute window, or an app that was never installed anywhere and so has no
installation to mint a token for.

Skipped unless GITHUB_APP_ID and a key are configured, so the default suite
stays offline.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.config import Settings
from app.github.auth import GitHubAppAuth, load_private_key
from app.github.client import GitHubClient

configured = bool(os.getenv("GITHUB_APP_ID")) and bool(
    os.getenv("GITHUB_PRIVATE_KEY") or os.getenv("GITHUB_PRIVATE_KEY_PATH")
)
pytestmark = pytest.mark.skipif(not configured, reason="GitHub App is not configured")

API = "https://api.github.com"


@pytest.fixture
def auth() -> GitHubAppAuth:
    s = Settings.from_env({**os.environ, "LLM_PROVIDER": "ollama"})
    key = load_private_key(s.github_private_key, s.github_private_key_path)
    return GitHubAppAuth(s.github_app_id, key, s.github_api)


async def _installation_id(auth: GitHubAppAuth) -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{API}/app/installations",
            headers={
                "Authorization": f"Bearer {auth.app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        installs = r.json()
    assert installs, "the app is not installed on any account"
    return int(installs[0]["id"])


async def test_github_accepts_the_app_jwt(auth):
    """A JWT signed with the wrong key or a mismatched app id fails only here."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{API}/app",
            headers={
                "Authorization": f"Bearer {auth.app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
    assert r.status_code == 200, r.text[:300]
    assert r.json()["slug"]


async def test_the_installation_grants_exactly_what_the_reviewer_needs(auth):
    """Over-permissioning is silent; under-permissioning fails deep in a review."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{API}/app/installations",
            headers={
                "Authorization": f"Bearer {auth.app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
    install = r.json()[0]
    assert install["permissions"].get("contents") == "read"
    assert install["permissions"].get("pull_requests") == "write"
    assert install["events"] == ["pull_request"]


async def test_a_token_is_minted_and_then_served_from_cache(auth):
    installation = await _installation_id(auth)

    token = await auth.installation_token(installation)
    assert token.startswith("ghs_")

    # Minting on every webhook would put a GitHub round trip in the hot path.
    assert await auth.installation_token(installation) == token


async def test_the_minted_token_can_read_a_file(auth):
    """contents:read is what lets the reviewer see more than the diff."""
    installation = await _installation_id(auth)
    token = await auth.installation_token(installation)

    gh = GitHubClient(token, API)
    try:
        body = await gh.fetch_file("tejaskumbhar288", "github-pr-review", "README.md", "main")
    finally:
        await gh.close()

    assert body and "AI Code Review Assistant" in body
