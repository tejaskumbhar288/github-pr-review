"""Webhook signature verification (roadmap 2).

GitHub signs every delivery with HMAC-SHA256 over the raw body. Verifying it is
the only thing standing between the public endpoint and anyone who can guess the
URL, so this compares in constant time and refuses to run at all when the secret
is unset - a webhook endpoint that silently accepts unsigned payloads is worse
than no endpoint.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
PREFIX = "sha256="


class SignatureError(ValueError):
    """The delivery was unsigned, malformed, or signed with the wrong secret."""


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return PREFIX + digest


def verify_signature(body: bytes, header: str | None, secret: str | None) -> None:
    """Raise SignatureError unless the body matches the signature header."""
    if not secret:
        raise SignatureError("webhook secret is not configured")
    if not header:
        raise SignatureError(f"missing {SIGNATURE_HEADER} header")
    if not header.startswith(PREFIX):
        raise SignatureError("signature header must be sha256=<hex>")

    expected = sign(body, secret)
    if not hmac.compare_digest(expected, header):
        raise SignatureError("signature mismatch")
