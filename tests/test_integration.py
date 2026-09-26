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


# --- incremental review, end to end (roadmap 1) ----------------------------


class RepushGitHub(FakeGitHub):
    """A PR that was already reviewed once, then pushed to."""

    def __init__(self, pr, contents, comparison):
        super().__init__(pr, contents)
        self._comparison = comparison
        self.compares: list[tuple[str, str]] = []

    async def compare(self, owner, repo, base, head, *, max_files, max_patch_lines):
        self.compares.append((base, head))
        return self._comparison


@pytest.fixture
def repushed(monkeypatch, pr, live_settings):
    from app.github.client import Comparison, PRFile
    from app.github.patch import parse_patch
    from app.github.publisher import marker_for

    increment = Comparison(
        status="ahead",
        ahead_by=2,
        commits=2,
        files=[
            PRFile(
                path="src/io.py",
                status="modified",
                additions=1,
                deletions=0,
                patch=parse_patch("@@ -30,1 +30,2 @@\n def later():\n+    return risky()\n"),
            )
        ],
    )
    gh = RepushGitHub(pr, {"src/io.py": "import os\n"}, increment)
    gh.reviews = [{"id": 1, "body": f"{marker_for('previous-sha')} an earlier review"}]
    llm = FakeProvider({"summary": "Adds a risky call.", "findings": []})

    original = ReviewSession.__init__
    monkeypatch.setattr(
        ReviewSession, "__init__", lambda self, s, **kw: original(self, s, gh=gh, llm=llm)
    )
    return gh, llm


async def test_a_repush_reviews_only_the_new_commits(live_settings, repushed):
    gh, llm = repushed

    async with ReviewSession(live_settings) as session:
        outcome = await session.run("acme", "widget", 7, post=True)

    assert gh.compares == [("previous-sha", "head456")]
    assert outcome.incremental == "narrowed"

    # The model saw the increment's diff, not the original PR's.
    prompt = llm.calls[0]["user"]
    assert "return risky()" in prompt
    assert "incremental review" in prompt.lower()
    assert "def save(path, data)" not in prompt

    # And the reader is told, above the summary.
    _, payload = gh.posted[0]
    assert "Incremental review" in payload["body"]
    assert "2 commit(s)" in payload["body"]


async def test_full_forces_a_complete_re_read(live_settings, repushed):
    gh, llm = repushed

    async with ReviewSession(live_settings) as session:
        outcome = await session.run("acme", "widget", 7, post=True, incremental=False)

    assert gh.compares == [], "--full must not spend a compare request"
    assert outcome.incremental == "full"
    assert "def save(path, data)" in llm.calls[0]["user"]


async def test_new_commits_touching_nothing_reviewable_cost_no_model_request(
    live_settings, repushed, monkeypatch
):
    """The whole point of the feature: a merge from main should not spend a
    request from a 20/day cap re-reporting the previous review."""
    from app.github.client import Comparison

    gh, llm = repushed
    gh._comparison = Comparison(status="ahead", ahead_by=1, commits=1, files=[])

    async with ReviewSession(live_settings) as session:
        outcome = await session.run("acme", "widget", 7, post=True)

    assert outcome.incremental == "nothing_new"
    assert llm.calls == [], "no model request"
    assert gh.posted == [], "and no second review saying nothing changed"
    assert outcome.published is not None and not outcome.published.posted
