"""GitHub App authentication (roadmap 2).

A GitHub App does not have a static token. It signs a short-lived JWT with its
private key, exchanges that for an installation access token, and that token
expires in an hour. Tokens are cached per installation and refreshed early,
because minting one on every webhook would add a round trip to the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# GitHub rejects a JWT with more than 10 minutes of life. Stay under it, and
# backdate `iat` to tolerate clock skew between us and GitHub.
JWT_TTL = 540
CLOCK_SKEW = 60
REFRESH_MARGIN = 300


class GitHubAuthError(RuntimeError):
    """The app could not authenticate as an installation."""


@dataclass
class InstallationToken:
    token: str
    expires_at: float

    @property
    def is_fresh(self) -> bool:
        return time.time() < self.expires_at - REFRESH_MARGIN


def load_private_key(inline: str | None, path: str | None) -> str:
    """Resolve the PEM from an env var or a file, tolerating \\n-escaped input."""
    if inline:
        key = inline.replace("\\n", "\n").strip()
        if "PRIVATE KEY" not in key:
            raise GitHubAuthError(
                "GITHUB_PRIVATE_KEY does not look like a PEM. Paste the whole file "
                "including the BEGIN/END lines."
            )
        return key + "\n"
    if path:
        pem = Path(path)
        if not pem.is_file():
            raise GitHubAuthError(f"GITHUB_PRIVATE_KEY_PATH does not exist: {path}")
        return pem.read_text(encoding="utf-8")
    raise GitHubAuthError("No GitHub App private key configured")


class GitHubAppAuth:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        api: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ):
        self._app_id = str(app_id)
        self._private_key = private_key
        self._api = api.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._cache: dict[int, InstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise GitHubAuthError(
                "PyJWT with crypto extras is required for GitHub App auth: "
                "pip install 'PyJWT[crypto]'"
            ) from exc

        now = int(time.time())
        payload = {"iat": now - CLOCK_SKEW, "exp": now + JWT_TTL, "iss": self._app_id}
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except Exception as exc:  # noqa: BLE001 - surfaces as a clear config error
            raise GitHubAuthError(f"could not sign the app JWT: {exc}") from exc

    async def installation_token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and cached.is_fresh:
            return cached.token

        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            # Another coroutine may have refreshed while we waited.
            cached = self._cache.get(installation_id)
            if cached and cached.is_fresh:
                return cached.token

            resp = await self._client.post(
                f"{self._api}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self.app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if resp.status_code == 401:
                raise GitHubAuthError(
                    "GitHub rejected the app JWT (401). Check GITHUB_APP_ID and that the "
                    "private key belongs to that app."
                )
            if resp.status_code == 404:
                raise GitHubAuthError(
                    f"installation {installation_id} not found (404) - the app may have "
                    "been uninstalled from that account."
                )
            if resp.status_code >= 400:
                raise GitHubAuthError(
                    f"token exchange failed {resp.status_code}: {resp.text[:300]}"
                )

            data = resp.json()
            token = data.get("token")
            if not token:
                raise GitHubAuthError("token exchange returned no token")

            self._cache[installation_id] = InstallationToken(
                token=token,
                # Trust our own clock over parsing their timestamp format.
                expires_at=time.time() + 3600,
            )
            log.debug("minted installation token for %s", installation_id)
            return token

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
