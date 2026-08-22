from __future__ import annotations

import asyncio

from app.server.queue import MemoryQueue, ReviewJob


def job(sha="abc", number=1):
    return ReviewJob(owner="acme", repo="widget", number=number, head_sha=sha)


async def test_enqueue_then_dequeue_roundtrips():
    q = MemoryQueue()
    assert await q.enqueue(job())
    got = await q.dequeue(timeout=1)
    assert got.owner == "acme" and got.head_sha == "abc"


def test_json_roundtrip_preserves_the_dedupe_key():
    j = job()
    assert ReviewJob.from_json(j.to_json()).dedupe_key == j.dedupe_key


def test_unknown_fields_in_stored_json_are_ignored():
    """Forward compatibility: a worker on an older build must not crash on a
    job enqueued by a newer one."""
    raw = '{"owner":"a","repo":"b","number":1,"head_sha":"s","future_field":true}'
    assert ReviewJob.from_json(raw).slug == "a/b#1"


async def test_the_same_commit_is_only_queued_once():
    q = MemoryQueue()
    assert await q.enqueue(job("abc"))
    assert not await q.enqueue(job("abc"))
    assert await q.depth() == 1


async def test_a_new_commit_is_a_new_job():
    q = MemoryQueue()
    await q.enqueue(job("abc"))
    assert await q.enqueue(job("def"))
    assert await q.depth() == 2


async def test_a_different_pr_is_a_new_job():
    q = MemoryQueue()
    await q.enqueue(job("abc", number=1))
    assert await q.enqueue(job("abc", number=2))


async def test_completion_holds_the_reservation():
    q = MemoryQueue()
    await q.enqueue(job())
    j = await q.dequeue(timeout=1)
    await q.complete(j)
    assert not await q.enqueue(job())


async def test_failure_releases_the_reservation_so_a_retry_can_run():
    """A transient LLM outage must not permanently poison a PR."""
    q = MemoryQueue()
    await q.enqueue(job())
    j = await q.dequeue(timeout=1)
    await q.fail(j, "provider down")
    assert await q.enqueue(job())


async def test_dequeue_times_out_rather_than_blocking_forever():
    q = MemoryQueue()
    assert await q.dequeue(timeout=0.05) is None


async def test_expired_reservations_allow_a_requeue():
    q = MemoryQueue()
    await q.enqueue(job())
    j = await q.dequeue(timeout=1)
    await q.complete(j)
    q._reservations[job().dedupe_key] = 0  # simulate TTL expiry
    assert await q.enqueue(job())


async def test_concurrent_enqueues_of_the_same_commit_admit_exactly_one():
    q = MemoryQueue()
    results = await asyncio.gather(*(q.enqueue(job("same")) for _ in range(20)))
    assert sum(results) == 1
    assert await q.depth() == 1
