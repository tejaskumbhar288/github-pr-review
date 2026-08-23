from __future__ import annotations

import time

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


async def test_a_repeatedly_failing_pr_is_dead_lettered_instead_of_retried(
    monkeypatch, settings, pr
):
    """Roadmap 4: without a give-up point, a PR that fails every time is retried
    on every redelivery forever and nothing but the log says so."""

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

    q = MemoryQueue(max_attempts=2)
    worker = ReviewWorker(settings, q)

    def job():
        return ReviewJob(owner="acme", repo="widget", number=7, head_sha="head456")

    await q.enqueue(job())
    assert not await worker.handle(await q.dequeue(timeout=1))
    assert worker.dead_lettered == 0

    # Second delivery of the same commit: this one exhausts the budget.
    assert await q.enqueue(job())
    assert not await worker.handle(await q.dequeue(timeout=1))
    assert worker.dead_lettered == 1

    assert await q.dead_depth() == 1
    assert not await q.enqueue(job()), "a dead-lettered commit stops being retried"
    assert (await q.dead_letters())[0]["error"] == "RuntimeError: model down"


async def test_the_queue_wait_is_reported_in_real_seconds(settings, caplog):
    """perf_counter counts from an arbitrary origin, time.time from the epoch.

    Subtracting one from the other logged a queue wait of -1787342661.8s on the
    first real webhook - a number so wrong it reads as a timestamp, which is
    exactly what it was.
    """
    import logging

    queue = MemoryQueue()
    job = ReviewJob(owner="o", repo="r", number=1, head_sha="deadbee")
    job.enqueued_at = time.time() - 3.0

    worker = ReviewWorker(settings, queue)
    with caplog.at_level(logging.INFO):
        try:
            await worker.handle(job)
        except Exception:
            pass

    waits = [
        float(m.split("queued ")[1].split("s ago")[0])
        for m in (r.getMessage() for r in caplog.records)
        if "queued " in m and "s ago" in m
    ]
    assert waits, "the worker never logged a queue wait"
    assert 0.0 <= waits[0] < 60.0, f"implausible queue wait: {waits[0]}"
