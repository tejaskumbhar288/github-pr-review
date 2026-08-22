from __future__ import annotations

import httpx
import pytest
import respx

from app.github.auth import GitHubAppAuth, GitHubAuthError, load_private_key

API = "https://api.github.com"

# A throwaway key generated for this test only.
KEY = None


def _key() -> str:
    global KEY
    if KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return KEY


def test_inline_key_accepts_escaped_newlines():
    escaped = _key().replace("\n", "\\n")
    assert "BEGIN PRIVATE KEY" in load_private_key(escaped, None)


def test_key_from_a_file(tmp_path):
    p = tmp_path / "app.pem"
    p.write_text(_key())
    assert load_private_key(None, str(p)) == _key()


def test_a_non_pem_value_is_rejected_clearly():
    with pytest.raises(GitHubAuthError, match="does not look like a PEM"):
        load_private_key("hunter2", None)


def test_a_missing_file_is_reported():
    with pytest.raises(GitHubAuthError, match="does not exist"):
        load_private_key(None, "/nope/app.pem")


def test_no_key_at_all_is_reported():
    with pytest.raises(GitHubAuthError, match="No GitHub App private key"):
        load_private_key(None, None)


def test_jwt_carries_the_app_id_and_a_bounded_lifetime():
    import jwt

    auth = GitHubAppAuth("12345", _key())
    token = auth.app_jwt()
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert 0 < claims["exp"] - claims["iat"] <= 600


@respx.mock
async def test_installation_token_is_fetched_and_cached():
    route = respx.post(f"{API}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc"})
    )
    auth = GitHubAppAuth("1", _key())
    try:
        assert await auth.installation_token(42) == "ghs_abc"
        assert await auth.installation_token(42) == "ghs_abc"
    finally:
        await auth.close()
    assert route.call_count == 1, "the second call must come from the cache"


@respx.mock
async def test_concurrent_callers_mint_only_one_token():
    import asyncio

    route = respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_x"})
    )
    auth = GitHubAppAuth("1", _key())
    try:
        tokens = await asyncio.gather(*(auth.installation_token(7) for _ in range(10)))
    finally:
        await auth.close()
    assert set(tokens) == {"ghs_x"}
    assert route.call_count == 1


@respx.mock
async def test_a_rejected_jwt_says_what_to_check():
    respx.post(f"{API}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(401, json={})
    )
    auth = GitHubAppAuth("1", _key())
    try:
        with pytest.raises(GitHubAuthError, match="GITHUB_APP_ID"):
            await auth.installation_token(42)
    finally:
        await auth.close()


@respx.mock
async def test_an_uninstalled_app_is_reported_clearly():
    respx.post(f"{API}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(404, json={})
    )
    auth = GitHubAppAuth("1", _key())
    try:
        with pytest.raises(GitHubAuthError, match="uninstalled"):
            await auth.installation_token(42)
    finally:
        await auth.close()
