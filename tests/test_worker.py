from __future__ import annotations

import pytest

from app.pipeline import ReviewSession
from app.server.queue import MemoryQueue, ReviewJob
from app.server.worker import ReviewWorker
from tests.conftest import FakeGitHub, FakeProvider, finding_payload


@pytest.fixture
def wired(monkeypatch, settings, pr):
    """Point ReviewSession at fakes so the worker exercises the real path."""
    gh = FakeGitHub(pr)
    llm = FakeProvider({"summary": "s", "findings": [finding_payload()]})

    original = ReviewSession.__init__

    def patched(self, s, **kwargs):
        kwargs.pop("token", None)
        original(self, s, gh=gh, llm=llm, **kwargs)

    monkeypatch.setattr(ReviewSession, "__init__", patched)
    return gh, llm


async def test_worker_reviews_and_publishes(wired, settings):
    gh, _ = wired
    q = MemoryQueue()
    job = ReviewJob(owner="acme", repo="widget", number=7, head_sha="head456")
    await q.enqueue(job)

    worker = ReviewWorker(settings, q)
    assert await worker.handle(await q.dequeue(timeout=1))

    assert worker.processed == 1 and worker.failed == 0
    assert gh.posted, "the worker must publish, unlike the CLI's default"
    assert gh.posted[0][1]["comments"][0]["line"] == 8


async def test_a_failing_review_does_not_kill_the_worker(monkeypatch, settings, pr):
    class Boom(FakeProvider):
        async def complete_json(self, **kwargs):
            raise RuntimeError("model down")

    gh = FakeGitHub(pr)
    original = ReviewSession.__init__
    monkeypatch.setattr(
        ReviewSession,
        "__init__",
        lambda self, s, **kw: original(self, s, gh=gh, llm=Boom()),
    )

    q = MemoryQueue()
    job = ReviewJob(owner="acme", repo="widget", number=7, head_sha="head456")
    await q.enqueue(job)

    worker = ReviewWorker(settings, q)
    assert not await worker.handle(await q.dequeue(timeout=1))
    assert worker.failed == 1

    # The reservation was released, so a redelivery can retry.
    assert await q.enqueue(job)


async def test_a_duplicate_is_detected_before_the_model_is_called(monkeypatch, settings, pr):
    """Regression: the duplicate check ran at publish time, so a redelivered
    webhook spent a full review (~45s, ~48k tokens) before discarding it."""
    from app.github.publisher import marker_for

    gh = FakeGitHub(pr)
    gh.reviews = [{"body": marker_for(pr.head_sha)}]
    llm = FakeProvider({"summary": "s", "findings": [finding_payload()]})

    original = ReviewSession.__init__
    monkeypatch.setattr(
        ReviewSession, "__init__", lambda self, s, **kw: original(self, s, gh=gh, llm=llm)
    )

    async with ReviewSession(settings) as session:
        outcome = await session.run_pr(pr, post=True)

    assert llm.calls == [], "the model must not be called for a duplicate"
    assert gh.posted == []
    assert outcome.published.skipped_reason == "already reviewed at this commit"


async def test_force_still_reviews_a_previously_reviewed_commit(monkeypatch, settings, pr):
    from app.github.publisher import marker_for

    gh = FakeGitHub(pr)
    gh.reviews = [{"body": marker_for(pr.head_sha)}]
    llm = FakeProvider({"summary": "s", "findings": [finding_payload()]})
    original = ReviewSession.__init__
    monkeypatch.setattr(
        ReviewSession, "__init__", lambda self, s, **kw: original(self, s, gh=gh, llm=llm)
    )

    async with ReviewSession(settings) as session:
        outcome = await session.run_pr(pr, post=True, skip_if_reviewed=False)

    assert len(llm.calls) == 1
    assert outcome.published.posted
