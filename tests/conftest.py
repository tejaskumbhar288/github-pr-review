from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.github.client import PRFile, PullRequest
from app.github.patch import parse_patch
from app.llm.base import LLMProvider, LLMResponse, TokenUsage

REPO_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_PATCH = """@@ -1,6 +1,10 @@
 import os
 
-def load(path):
-    return open(path).read()
+def load(path):
+    with open(path) as fh:
+        return fh.read()
+
+def save(path, data):
+    open(path, "w").write(data)
"""


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(
        {
            "LLM_PROVIDER": "ollama",
            "STATIC_ANALYSIS": "false",
            "CONTEXT_CHAR_LIMIT": "0",
            "GITHUB_WEBHOOK_SECRET": "test-secret",
        }
    )


@pytest.fixture
def pr() -> PullRequest:
    return PullRequest(
        owner="acme",
        repo="widget",
        number=7,
        title="Tidy up file IO",
        body="",
        base_sha="base123",
        head_sha="head456",
        files=[
            PRFile(
                path="src/io.py",
                status="modified",
                additions=6,
                deletions=2,
                patch=parse_patch(SAMPLE_PATCH),
            )
        ],
    )


class FakeProvider(LLMProvider):
    """Returns a canned payload and records what it was asked."""

    name = "fake"

    def __init__(self, payload: dict[str, Any] | None = None, usage: TokenUsage | None = None):
        self.model = "fake-1"
        self.payload = payload if payload is not None else {"summary": "ok", "findings": []}
        self.usage = usage or TokenUsage(prompt_tokens=100, completion_tokens=20)
        self.calls: list[dict[str, str]] = []
        self.closed = False

    async def complete_json(self, *, system: str, user: str, temperature: float = 0.2):
        self.calls.append({"system": system, "user": user})
        return LLMResponse(
            data=self.payload,
            model=self.model,
            provider=self.name,
            usage=self.usage,
            latency_ms=12.5,
        )

    async def close(self) -> None:
        self.closed = True


class FakeGitHub:
    """Stands in for GitHubClient without touching the network."""

    def __init__(self, pr: PullRequest, contents: dict[str, str] | None = None):
        self._pr = pr
        self._contents = contents or {}
        self.posted: list[tuple[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.raise_on_post: Exception | None = None

    async def fetch_pr(self, owner, repo, number, *, max_files, max_patch_lines):
        return self._pr

    async def fetch_file(self, owner, repo, path, ref):
        return self._contents.get(path)

    async def get_json(self, path: str, **params: Any):
        if path.endswith("/reviews"):
            return self.reviews
        return {}

    async def post_json(self, path: str, payload: Any):
        if self.raise_on_post is not None:
            exc, self.raise_on_post = self.raise_on_post, None
            raise exc
        self.posted.append((path, payload))
        return {"id": 999, "html_url": "https://github.com/acme/widget/pull/7#review-999"}

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def fake_gh(pr: PullRequest) -> FakeGitHub:
    return FakeGitHub(pr)


def finding_payload(**over: Any) -> dict[str, Any]:
    base = {
        "file": "src/io.py",
        "line": 8,
        "severity": "major",
        "category": "correctness",
        "title": "Unclosed handle in save()",
        "detail": "open() without a context manager leaks the descriptor.",
        "suggestion": 'with open(path, "w") as fh:\n    fh.write(data)',
        "confidence": 0.9,
    }
    base.update(over)
    return base


@pytest.fixture
def webhook_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "draft": False,
            "user": {"type": "User", "login": "dev"},
            "head": {"sha": "head456"},
        },
        "repository": {"full_name": "acme/widget", "name": "widget"},
        "installation": {"id": 42},
    }


@pytest.fixture
def webhook_body(webhook_payload) -> bytes:
    return json.dumps(webhook_payload).encode()


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff instant. Defined without calling the real sleep, so the
    patched function cannot recurse into itself."""
    import asyncio as _asyncio

    async def _instant(_delay):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _instant)
    return _instant
