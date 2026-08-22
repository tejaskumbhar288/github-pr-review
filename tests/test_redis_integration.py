"""RedisQueue against a real Redis.

MemoryQueue covers the semantics, but the Redis implementation has its own
failure modes - BRPOPLPUSH handoff, SET NX reservations, orphan recovery - that
only a real server exercises. Skipped when REDIS_TEST_URL is unset, so the
default suite stays offline.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.server.queue import POLL_TIMEOUT, RedisQueue, ReviewJob, build_queue

URL = os.getenv("REDIS_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="REDIS_TEST_URL is not set")


@pytest.fixture
async def queue():
    import redis.asyncio as aioredis

    client = aioredis.from_url(URL, decode_responses=True)
    name = f"test-{uuid.uuid4().hex[:8]}"
    q = RedisQueue(client, name, ttl=60)
    yield q
    await client.delete(q._key, q._processing, q._dead_key)
    for pattern in ("review:seen:*", "review:fails:*"):
        for key in await client.keys(pattern):
            await client.delete(key)
    await client.aclose()


@pytest.fixture
async def strict_queue():
    """A queue that dead-letters on the first failure, so the give-up path can
    be exercised without three round trips per assertion."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(URL, decode_responses=True)
    name = f"test-{uuid.uuid4().hex[:8]}"
    q = RedisQueue(client, name, ttl=60, max_attempts=1)
    yield q
    await client.delete(q._key, q._processing, q._dead_key)
    for pattern in ("review:seen:*", "review:fails:*"):
        for key in await client.keys(pattern):
            await client.delete(key)
    await client.aclose()


def job(sha="abc", number=1):
    return ReviewJob(owner="acme", repo="widget", number=number, head_sha=sha)


async def test_roundtrip(queue):
    assert await queue.enqueue(job())
    got = await queue.dequeue(timeout=1)
    assert got.owner == "acme" and got.head_sha == "abc"
    await queue.complete(got)


async def test_the_same_commit_is_reserved_once(queue):
    assert await queue.enqueue(job("sha1"))
    assert not await queue.enqueue(job("sha1"))
    assert await queue.depth() == 1


async def test_a_new_commit_is_a_new_job(queue):
    await queue.enqueue(job("sha1"))
    assert await queue.enqueue(job("sha2"))
    assert await queue.depth() == 2


async def test_concurrent_enqueues_admit_exactly_one(queue):
    """SET NX is the actual guard against a webhook storm; prove it."""
    results = await asyncio.gather(*(queue.enqueue(job("race")) for _ in range(25)))
    assert sum(results) == 1
    assert await queue.depth() == 1


async def test_failure_releases_the_reservation(queue):
    await queue.enqueue(job("sha3"))
    j = await queue.dequeue(timeout=1)
    await queue.fail(j, "provider down")
    assert await queue.enqueue(job("sha3"))


async def test_completion_holds_the_reservation(queue):
    await queue.enqueue(job("sha4"))
    j = await queue.dequeue(timeout=1)
    await queue.complete(j)
    assert not await queue.enqueue(job("sha4"))


async def test_dequeued_job_sits_in_processing_until_completed(queue):
    await queue.enqueue(job("sha5"))
    j = await queue.dequeue(timeout=1)
    assert await queue._redis.llen(queue._processing) == 1
    await queue.complete(j)
    assert await queue._redis.llen(queue._processing) == 0


async def test_a_crashed_worker_orphan_is_requeued(queue):
    """The whole point of the processing list: a dead worker loses nothing."""
    await queue.enqueue(job("sha6"))
    await queue.dequeue(timeout=1)  # simulate a worker that dies here
    assert await queue.depth() == 0

    assert await queue.requeue_stale() == 1
    assert await queue.depth() == 1
    recovered = await queue.dequeue(timeout=1)
    assert recovered.head_sha == "sha6"


async def test_empty_queue_times_out(queue):
    assert await queue.dequeue(timeout=1) is None


async def test_an_idle_poll_at_the_real_timeout_returns_none(settings):
    """Regression: the worker's actual POLL_TIMEOUT raced redis-py's default
    5s socket_timeout, so every idle poll raised TimeoutError and the consumer
    loop spun on errors. The original unit test used timeout=1 and missed it."""
    q = await build_queue(settings.with_overrides(redis_url=URL))
    try:
        assert q.name == "redis"
        assert await q.dequeue(timeout=POLL_TIMEOUT) is None
    finally:
        await q.close()


async def test_a_worker_polling_an_idle_queue_logs_no_errors(settings, caplog):
    """The observable symptom: ERROR lines on a perfectly healthy idle queue."""
    import logging

    from app.server.worker import ReviewWorker

    q = await build_queue(settings.with_overrides(redis_url=URL))
    worker = ReviewWorker(settings, q)
    try:
        with caplog.at_level(logging.ERROR):
            consumer = asyncio.create_task(worker._consume(0))
            await asyncio.sleep(POLL_TIMEOUT + 2)
            worker.stop()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        assert [r.message for r in caplog.records if r.levelno >= logging.ERROR] == []
    finally:
        await q.close()


async def test_malformed_payload_is_discarded_not_crashed(queue):
    await queue._redis.lpush(queue._key, "{not json")
    assert await queue.dequeue(timeout=1) is None
    assert await queue._redis.llen(queue._processing) == 0


async def test_build_queue_picks_redis_when_reachable(settings):
    q = await build_queue(settings.with_overrides(redis_url=URL))
    try:
        assert q.name == "redis"
    finally:
        await q.close()


async def test_build_queue_falls_back_when_redis_is_down(settings):
    q = await build_queue(settings.with_overrides(redis_url="redis://127.0.0.1:1/0"))
    assert q.name == "memory"


async def test_a_socket_read_timeout_is_treated_as_an_empty_poll():
    """Force the exact failure the socket_timeout fix prevents, and prove the
    handler catches it. redis.exceptions.TimeoutError shadows the builtin name
    without subclassing it, so `except TimeoutError` would catch nothing here.
    """
    import redis.asyncio as aioredis
    import redis.exceptions

    from app.server.queue import IDLE_POLL_ERRORS

    assert redis.exceptions.TimeoutError in IDLE_POLL_ERRORS

    # socket_timeout below the blocking timeout guarantees the read deadline hits.
    client = aioredis.from_url(URL, decode_responses=True, socket_timeout=1)
    q = RedisQueue(client, f"timeout-{uuid.uuid4().hex[:8]}", ttl=60)
    try:
        # Confirm the raw client really does raise, so the test is not vacuous.
        with pytest.raises(redis.exceptions.TimeoutError):
            await client.brpoplpush(q._key, q._processing, timeout=5)

        # The queue swallows it and reports an empty poll.
        assert await q.dequeue(timeout=5) is None
    finally:
        await client.aclose()


# --- dead-letter queue ----------------------------------------------------


async def test_the_failure_counter_survives_a_new_job_object(queue):
    """The whole point of counting on the key: a redelivery is a fresh
    ReviewJob with attempts=0, and INCR is what remembers the streak."""
    for expected in (1, 2):
        await queue.enqueue(job("sha1"))
        j = await queue.dequeue(timeout=1)
        assert not await queue.fail(j, "provider down")
        assert int(await queue._redis.get(queue._fail_key(j.dedupe_key))) == expected


async def test_a_job_is_dead_lettered_once_it_exhausts_its_attempts(strict_queue):
    q = strict_queue
    await q.enqueue(job("sha1"))
    assert await q.fail(await q.dequeue(timeout=1), "GitHubError: 404") is True

    assert await q.dead_depth() == 1
    (record,) = await q.dead_letters()
    assert record["job"]["head_sha"] == "sha1"
    assert record["error"] == "GitHubError: 404"
    assert record["attempts"] == 1


async def test_a_dead_lettered_commit_keeps_its_reservation(strict_queue):
    q = strict_queue
    await q.enqueue(job("sha1"))
    await q.fail(await q.dequeue(timeout=1), "boom")
    assert not await q.enqueue(job("sha1")), "redeliveries must stop re-running it"
    assert await q.depth() == 0


async def test_a_dead_lettered_job_leaves_the_processing_list_clean(strict_queue):
    q = strict_queue
    await q.enqueue(job("sha1"))
    await q.fail(await q.dequeue(timeout=1), "boom")
    assert await q._redis.llen(q._processing) == 0


async def test_a_success_clears_the_failure_counter(queue):
    await queue.enqueue(job("sha1"))
    j = await queue.dequeue(timeout=1)
    await queue.fail(j, "transient")

    await queue.enqueue(job("sha1"))
    await queue.complete(await queue.dequeue(timeout=1))
    assert await queue._redis.get(queue._fail_key(j.dedupe_key)) is None


async def test_draining_requeues_with_a_fresh_budget(strict_queue):
    q = strict_queue
    await q.enqueue(job("sha1"))
    await q.fail(await q.dequeue(timeout=1), "boom")

    assert await q.drain_dead_letters() == 1
    assert await q.dead_depth() == 0
    assert await q.depth() == 1
    # Both the reservation and the count were cleared, so the retry actually
    # gets to run instead of being dead-lettered again on contact.
    j = await q.dequeue(timeout=1)
    assert j.head_sha == "sha1"
    assert await q._redis.get(q._fail_key(j.dedupe_key)) is None


async def test_an_orphan_counts_as_a_failed_attempt(queue):
    """A job that hard-crashes its worker never reaches fail(), so orphan
    recovery is the only place the attempt can be counted. Without this a
    poison pill is recovered, re-crashes and loops forever."""
    await queue.enqueue(job("sha1"))
    j = await queue.dequeue(timeout=1)  # now sitting in the processing list

    assert await queue.requeue_stale() == 1
    assert int(await queue._redis.get(queue._fail_key(j.dedupe_key))) == 1
    assert await queue.depth() == 1


async def test_a_poison_pill_orphan_is_quarantined_not_requeued(strict_queue):
    q = strict_queue
    await q.enqueue(job("sha1"))
    await q.dequeue(timeout=1)

    assert await q.requeue_stale() == 0, "it must not go back on the queue"
    assert await q.depth() == 0
    assert await q.dead_depth() == 1
    assert (await q.dead_letters())[0]["error"] == "orphaned by a crashed worker"


async def test_a_healthy_orphan_still_comes_back(queue):
    await queue.enqueue(job("sha1"))
    await queue.dequeue(timeout=1)

    assert await queue.requeue_stale() == 1
    assert await queue.dead_depth() == 0
    assert (await queue.dequeue(timeout=1)).head_sha == "sha1"


async def test_a_malformed_orphan_is_dropped_rather_than_looping(queue):
    await queue._redis.lpush(queue._processing, "{not json")
    assert await queue.requeue_stale() == 0
    assert await queue.depth() == 0
    assert await queue.dead_depth() == 0
