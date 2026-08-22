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


# --- dead-letter queue ----------------------------------------------------


async def test_a_job_is_retried_until_it_exhausts_its_attempts():
    """The retry budget is per dedupe key, not per job object: a redelivery
    arrives as a brand new ReviewJob, so counting on the job would reset the
    streak every time and never reach the limit."""
    q = MemoryQueue(max_attempts=3)
    for attempt in range(2):
        assert await q.enqueue(job()), f"attempt {attempt} should have been accepted"
        j = await q.dequeue(timeout=1)
        assert not await q.fail(j, "provider down")

    assert await q.enqueue(job())
    j = await q.dequeue(timeout=1)
    assert await q.fail(j, "provider down") is True
    assert await q.dead_depth() == 1


async def test_a_dead_lettered_job_keeps_its_reservation():
    """A fourth delivery of a job that failed three times is not new
    information, and re-running it burns model quota to fail again."""
    q = MemoryQueue(max_attempts=1)
    await q.enqueue(job())
    await q.fail(await q.dequeue(timeout=1), "boom")
    assert not await q.enqueue(job())


async def test_a_success_clears_the_failure_streak():
    q = MemoryQueue(max_attempts=2)
    await q.enqueue(job())
    await q.fail(await q.dequeue(timeout=1), "transient")

    await q.enqueue(job())
    await q.complete(await q.dequeue(timeout=1))

    # The next failure on this key is a first failure, not the second of a pair.
    q._reservations.clear()
    await q.enqueue(job())
    assert not await q.fail(await q.dequeue(timeout=1), "transient")


async def test_the_dead_letter_records_what_is_needed_to_act_on_it():
    q = MemoryQueue(max_attempts=1)
    await q.enqueue(job(sha="deadbeef", number=42))
    await q.fail(await q.dequeue(timeout=1), "GitHubError: 404")

    (record,) = await q.dead_letters()
    assert record["job"]["number"] == 42
    assert record["job"]["head_sha"] == "deadbeef"
    assert record["error"] == "GitHubError: 404"
    assert record["attempts"] == 1
    assert record["died_at"] > 0


async def test_dead_letters_are_listed_newest_first():
    q = MemoryQueue(max_attempts=1)
    for n in (1, 2, 3):
        await q.enqueue(job(sha=f"sha{n}", number=n))
        await q.fail(await q.dequeue(timeout=1), f"boom {n}")

    assert [r["job"]["number"] for r in await q.dead_letters()] == [3, 2, 1]
    assert [r["job"]["number"] for r in await q.dead_letters(limit=2)] == [3, 2]


async def test_draining_puts_jobs_back_and_gives_them_a_fresh_budget():
    """Requeueing without clearing the count would dead-letter the job again on
    its first retry, having never actually retried it."""
    q = MemoryQueue(max_attempts=1)
    await q.enqueue(job())
    await q.fail(await q.dequeue(timeout=1), "boom")

    assert await q.drain_dead_letters() == 1
    assert await q.dead_depth() == 0
    assert await q.depth() == 1

    j = await q.dequeue(timeout=1)
    assert j.dedupe_key == job().dedupe_key


async def test_draining_can_take_only_the_newest():
    q = MemoryQueue(max_attempts=1)
    for n in (1, 2):
        await q.enqueue(job(sha=f"sha{n}", number=n))
        await q.fail(await q.dequeue(timeout=1), "boom")

    assert await q.drain_dead_letters(limit=1) == 1
    assert await q.dead_depth() == 1
    assert (await q.dead_letters())[0]["job"]["number"] == 1


async def test_the_dead_letter_list_is_bounded():
    from app.server.queue import DEAD_LETTER_MAX

    q = MemoryQueue(max_attempts=1)
    for n in range(DEAD_LETTER_MAX + 5):
        await q.enqueue(job(sha=f"sha{n}", number=n))
        await q.fail(await q.dequeue(timeout=1), "boom")

    assert await q.dead_depth() == DEAD_LETTER_MAX
    # The newest survive; the oldest are what fall off.
    assert (await q.dead_letters())[0]["job"]["number"] == DEAD_LETTER_MAX + 4
