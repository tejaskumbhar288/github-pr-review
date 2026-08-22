"""End-to-end: a signed webhook delivery becomes a published review.

Exercises the real receiver, the real queue, the real worker, the real engine
and the real publisher together - only the network boundaries (GitHub, the LLM)
are faked. This is the test that would catch the CLI and the webhook path
drifting apart.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.pipeline import ReviewSession
from app.server.app import create_app
from app.server.queue import MemoryQueue
from app.server.security import sign
from app.server.worker import ReviewWorker
from tests.conftest import FakeGitHub, FakeProvider, finding_payload

SECRET = "test-secret"


@pytest.fixture
def live_settings(settings):
    """The default test settings disable whole-file context; this path wants it."""
    return settings.with_overrides(context_char_limit=60_000)


@pytest.fixture
def wired(monkeypatch, pr):
    gh = FakeGitHub(pr, {"src/io.py": "import os\n"})
    llm = FakeProvider(
        {
            "summary": "Adds save(), which leaks a file handle.",
            "findings": [
                finding_payload(severity="critical"),
                finding_payload(line=4242, title="ghost finding"),
            ],
        }
    )
    original = ReviewSession.__init__
    monkeypatch.setattr(
        ReviewSession, "__init__", lambda self, s, **kw: original(self, s, gh=gh, llm=llm)
    )
    return gh, llm


async def test_delivery_to_published_review(live_settings, webhook_payload, wired):
    gh, llm = wired
    queue = MemoryQueue()
    client = TestClient(create_app(live_settings, queue=queue))

    body = json.dumps(webhook_payload).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body, SECRET),
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["key"] == "acme/widget#7@head456"

    worker = ReviewWorker(live_settings, queue)
    job = await queue.dequeue(timeout=1)
    assert await worker.handle(job)

    # The prompt carried both the annotated diff and the whole-file context.
    prompt = llm.calls[0]["user"]
    assert "```diff" in prompt and "full file at HEAD" in prompt

    # The review was published, with the invented finding filtered out first.
    assert len(gh.posted) == 1
    _, payload = gh.posted[0]
    assert payload["commit_id"] == "head456"
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["line"] == 8
    assert "ghost finding" not in json.dumps(payload)
    assert "1 critical" in payload["body"]

    # And a redelivery of the same commit does nothing at all.
    duplicate = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body, SECRET),
            "X-GitHub-Delivery": "delivery-2",
            "Content-Type": "application/json",
        },
    )
    assert duplicate.json()["status"] == "duplicate"
    assert len(gh.posted) == 1


async def test_forged_delivery_never_reaches_the_worker(live_settings, webhook_payload, wired):
    gh, _ = wired
    queue = MemoryQueue()
    client = TestClient(create_app(live_settings, queue=queue))

    body = json.dumps(webhook_payload).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign(body, "wrong-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401
    assert await queue.depth() == 0
    assert gh.posted == []
