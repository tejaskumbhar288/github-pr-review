"""Job queue and idempotency (roadmap 3).

GitHub redelivers webhooks - on its own retry schedule, and on demand from the
UI - so the same push can arrive several times. The dedupe key is
``(owner/repo#number, head_sha)``: a redelivery for a commit already reviewed is
dropped, but a *new* commit on the same PR is a genuinely new job.

Reservations are released when a job fails, so a transient LLM outage doesn't
permanently poison a PR. Jobs move through a processing list rather than a bare
BRPOP, so a worker crash doesn't silently lose the review.

Releasing on failure has an obvious failure mode of its own: a PR that fails
*every* time is retried on every redelivery, forever, and nothing but the log
records that it never succeeded. So failures are counted per dedupe key, and the
job that exhausts ``max_attempts`` is moved to a dead-letter list and keeps its
reservation instead of being handed back. It stops burning quota, it stops
looking like an idle queue, and it is inspectable with ``make dlq``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# How long a reservation lives while a job is still in flight. Short enough that
# a crashed worker's PR becomes reviewable again quickly.
INFLIGHT_TTL = 1800

# How long a consumer blocks waiting for a job.
POLL_TIMEOUT = 5.0

# A job that has failed this many times is dead-lettered rather than retried.
# Overridden by Settings.max_attempts; this is the value for a bare queue.
MAX_ATTEMPTS = 3

# The dead-letter list is a diagnostic, not storage. Keep the newest entries and
# let the rest go, so a pathological repo cannot grow it without bound.
DEAD_LETTER_MAX = 500

# Ceiling on a single orphan-recovery sweep. A processing list longer than this
# means something is wrong that requeueing will not fix.
MAX_REQUEUE = 1000

# redis-py defaults socket_timeout to 5s, so a blocking read of exactly
# POLL_TIMEOUT races that deadline and loses - the client raises TimeoutError
# instead of returning "nothing arrived". The socket must outlive the block by a
# clear margin, or every idle poll looks like an outage.
SOCKET_TIMEOUT_MARGIN = 15.0


class _NeverRaised(Exception):
    """Placeholder so the except clause stays valid when redis is not installed."""


# redis.exceptions.TimeoutError shadows the builtin name but does NOT subclass
# it, so `except TimeoutError` silently catches nothing. Bind the real class.
try:
    from redis.exceptions import TimeoutError as RedisTimeoutError
except ImportError:  # pragma: no cover - redis is optional for MemoryQueue
    RedisTimeoutError = _NeverRaised

IDLE_POLL_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    asyncio.TimeoutError,
    RedisTimeoutError,
)


@dataclass
class ReviewJob:
    owner: str
    repo: str
    number: int
    head_sha: str
    installation_id: int | None = None
    delivery_id: str = ""
    action: str = ""
    enqueued_at: float = field(default_factory=time.time)
    attempts: int = 0
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def dedupe_key(self) -> str:
        return f"{self.slug}@{self.head_sha}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> ReviewJob:
        data = json.loads(raw)
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})


def dead_record(job: ReviewJob, error: str, attempts: int) -> dict[str, Any]:
    """What a dead-lettered job needs to carry to be actionable later.

    The whole job, so it can be requeued verbatim; the error, so the reason is
    visible without digging through logs; and the time, because "this has been
    dead for a week" and "this died in the last deploy" are different problems.
    """
    return {
        "job": asdict(job),
        "error": error,
        "attempts": attempts,
        "died_at": time.time(),
    }


class JobQueue(Protocol):
    async def enqueue(self, job: ReviewJob) -> bool: ...
    async def dequeue(self, timeout: float = POLL_TIMEOUT) -> ReviewJob | None: ...
    async def complete(self, job: ReviewJob) -> None: ...
    async def fail(self, job: ReviewJob, error: str) -> bool: ...
    async def depth(self) -> int: ...
    async def dead_depth(self) -> int: ...
    async def dead_letters(self, limit: int = 20) -> list[dict[str, Any]]: ...
    async def drain_dead_letters(self, limit: int = 0) -> int: ...
    async def close(self) -> None: ...


class MemoryQueue:
    """In-process queue. Correct for a single process; the default when Redis
    is unavailable, so `python -m app.server.worker` works with no infra."""

    def __init__(self, ttl: int = 86_400, max_attempts: int = MAX_ATTEMPTS):
        self._queue: asyncio.Queue[ReviewJob] = asyncio.Queue()
        self._reservations: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._dead: list[dict[str, Any]] = []
        self._ttl = ttl
        self._max_attempts = max_attempts
        self.name = "memory"

    def _reserve(self, key: str, ttl: int) -> bool:
        now = time.time()
        expiry = self._reservations.get(key)
        if expiry is not None and expiry > now:
            return False
        self._reservations[key] = now + ttl
        return True

    async def enqueue(self, job: ReviewJob) -> bool:
        if not self._reserve(job.dedupe_key, INFLIGHT_TTL):
            log.info("duplicate job dropped: %s", job.dedupe_key)
            return False
        await self._queue.put(job)
        return True

    async def dequeue(self, timeout: float = POLL_TIMEOUT) -> ReviewJob | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def complete(self, job: ReviewJob) -> None:
        self._reservations[job.dedupe_key] = time.time() + self._ttl
        # A success clears the history: the next failure on this commit is a
        # first failure, not the continuation of an old streak.
        self._failures.pop(job.dedupe_key, None)

    async def fail(self, job: ReviewJob, error: str) -> bool:
        attempts = self._failures.get(job.dedupe_key, 0) + 1
        self._failures[job.dedupe_key] = attempts
        if attempts >= self._max_attempts:
            job.attempts = attempts
            self._dead.append(dead_record(job, error, attempts))
            del self._dead[:-DEAD_LETTER_MAX]
            # Hold the reservation. A fourth delivery of a job that failed
            # three times is not new information.
            self._reservations[job.dedupe_key] = time.time() + self._ttl
            log.error(
                "job %s dead-lettered after %d attempt(s): %s", job.dedupe_key, attempts, error
            )
            return True
        self._reservations.pop(job.dedupe_key, None)
        log.warning(
            "job %s failed (attempt %d/%d): %s", job.dedupe_key, attempts, self._max_attempts, error
        )
        return False

    async def depth(self) -> int:
        return self._queue.qsize()

    async def dead_depth(self) -> int:
        return len(self._dead)

    async def dead_letters(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(reversed(self._dead[-limit:])) if limit else list(reversed(self._dead))

    async def drain_dead_letters(self, limit: int = 0) -> int:
        """Put dead-lettered jobs back on the queue, newest first.

        Requeueing clears both the failure count and the reservation, because
        the operator draining the list is asserting that whatever broke is
        fixed - otherwise the job would just be dead-lettered again on its
        first retry, having never been given one.
        """
        taken = self._dead[-limit:] if limit else self._dead[:]
        del self._dead[len(self._dead) - len(taken) :]
        for record in reversed(taken):
            job = ReviewJob.from_json(json.dumps(record["job"]))
            self._failures.pop(job.dedupe_key, None)
            self._reservations.pop(job.dedupe_key, None)
            await self.enqueue(job)
        return len(taken)

    async def close(self) -> None:
        return None


class RedisQueue:
    """Redis-backed queue with crash-safe handoff via a processing list."""

    def __init__(
        self,
        redis: Any,
        queue_name: str = "reviews",
        ttl: int = 86_400,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self._redis = redis
        self._key = f"queue:{queue_name}"
        self._processing = f"queue:{queue_name}:processing"
        self._dead_key = f"queue:{queue_name}:dead"
        self._ttl = ttl
        self._max_attempts = max_attempts
        self.name = "redis"

    def _dedupe_key(self, job: ReviewJob) -> str:
        return f"review:seen:{job.dedupe_key}"

    def _fail_key(self, dedupe_key: str) -> str:
        return f"review:fails:{dedupe_key}"

    async def _count_failure(self, dedupe_key: str) -> int:
        """Count one failure against a dedupe key and return the running total.

        The counter lives on the key, not the job, because a redelivery arrives
        as a brand new ReviewJob with attempts=0 - counting on the job would
        reset the streak on every retry and never reach the limit.
        """
        key = self._fail_key(dedupe_key)
        attempts = int(await self._redis.incr(key))
        await self._redis.expire(key, self._ttl)
        return attempts

    async def enqueue(self, job: ReviewJob) -> bool:
        reserved = await self._redis.set(
            self._dedupe_key(job), job.job_id, nx=True, ex=INFLIGHT_TTL
        )
        if not reserved:
            log.info("duplicate job dropped: %s", job.dedupe_key)
            return False
        await self._redis.lpush(self._key, job.to_json())
        return True

    async def dequeue(self, timeout: float = POLL_TIMEOUT) -> ReviewJob | None:
        try:
            raw = await self._redis.brpoplpush(
                self._key, self._processing, timeout=int(max(1, timeout))
            )
        except IDLE_POLL_ERRORS:
            # An idle queue, not a broken one. Returning None keeps the consumer
            # loop quiet instead of logging an error on every empty poll.
            return None
        if raw is None:
            return None
        try:
            return ReviewJob.from_json(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            log.error("malformed job dropped: %s", exc)
            await self._redis.lrem(self._processing, 1, raw)
            return None

    async def complete(self, job: ReviewJob) -> None:
        await self._redis.lrem(self._processing, 1, job.to_json())
        # Promote the reservation to the full window: this commit is done.
        await self._redis.set(self._dedupe_key(job), job.job_id, ex=self._ttl)
        await self._redis.delete(self._fail_key(job.dedupe_key))

    async def fail(self, job: ReviewJob, error: str) -> bool:
        await self._redis.lrem(self._processing, 1, job.to_json())
        attempts = await self._count_failure(job.dedupe_key)
        if attempts >= self._max_attempts:
            await self._dead_letter(job, error, attempts)
            return True
        # Release so a redelivery (or a manual retry) can pick it up again.
        await self._redis.delete(self._dedupe_key(job))
        log.warning(
            "job %s failed (attempt %d/%d): %s", job.dedupe_key, attempts, self._max_attempts, error
        )
        return False

    async def _dead_letter(self, job: ReviewJob, error: str, attempts: int) -> None:
        job.attempts = attempts
        await self._redis.lpush(self._dead_key, json.dumps(dead_record(job, error, attempts)))
        await self._redis.ltrim(self._dead_key, 0, DEAD_LETTER_MAX - 1)
        # Hold the reservation so redeliveries stop re-running a review that has
        # already failed max_attempts times.
        await self._redis.set(self._dedupe_key(job), job.job_id, ex=self._ttl)
        log.error("job %s dead-lettered after %d attempt(s): %s", job.dedupe_key, attempts, error)

    async def requeue_stale(self) -> int:
        """Move anything left in the processing list back onto the queue.

        Called at worker startup: whatever is sitting there belongs to a worker
        that died mid-review.

        An orphan counts as a failed attempt. A job that hard-crashes its worker
        never reaches ``fail()``, so without this a poison pill is recovered,
        re-crashes, is recovered again, and loops for as long as the service
        runs. Counting orphans is what lets that job reach the dead letter list.
        """
        counts = {"requeued": 0, "dead": 0, "malformed": 0}
        while (raw := await self._redis.rpoplpush(self._processing, self._key)) is not None:
            counts[await self._triage_orphan(raw)] += 1
            if sum(counts.values()) >= MAX_REQUEUE:
                log.error(
                    "stopped orphan recovery at %d job(s); processing list is not draining",
                    MAX_REQUEUE,
                )
                break
        if counts["requeued"]:
            log.warning("requeued %d job(s) orphaned by a previous worker", counts["requeued"])
        if counts["dead"]:
            log.error(
                "dead-lettered %d orphaned job(s) that had exhausted their attempts",
                counts["dead"],
            )
        return counts["requeued"]

    async def _triage_orphan(self, raw: str | bytes) -> str:
        """Decide what a just-requeued orphan deserves, and undo the requeue if
        the answer is not "another try".

        The move itself stays a single atomic RPOPLPUSH, so a crash here can
        only ever leave the job on the queue - never nowhere. The worst case is
        that it is recovered once more on the next startup.
        """
        try:
            job = ReviewJob.from_json(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            log.error("malformed orphan dropped: %s", exc)
            await self._redis.lrem(self._key, 1, raw)
            return "malformed"

        attempts = await self._count_failure(job.dedupe_key)
        if attempts < self._max_attempts:
            return "requeued"
        await self._redis.lrem(self._key, 1, raw)
        await self._dead_letter(job, "orphaned by a crashed worker", attempts)
        return "dead"

    async def depth(self) -> int:
        return int(await self._redis.llen(self._key))

    async def dead_depth(self) -> int:
        return int(await self._redis.llen(self._dead_key))

    async def dead_letters(self, limit: int = 20) -> list[dict[str, Any]]:
        end = limit - 1 if limit else -1
        raw = await self._redis.lrange(self._dead_key, 0, end)
        out = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                log.warning("skipping malformed dead-letter entry")
        return out

    async def drain_dead_letters(self, limit: int = 0) -> int:
        """Put dead-lettered jobs back on the queue, newest first.

        Requeueing clears both the failure count and the reservation, because
        the operator draining the list is asserting that whatever broke is
        fixed - otherwise the job would just be dead-lettered again on its
        first retry, having never been given one.
        """
        drained = 0
        while limit == 0 or drained < limit:
            raw = await self._redis.rpop(self._dead_key)
            if raw is None:
                break
            drained += 1
            try:
                record = json.loads(raw)
                job = ReviewJob.from_json(json.dumps(record["job"]))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.error("dropped malformed dead-letter entry: %s", exc)
                continue
            await self._redis.delete(self._fail_key(job.dedupe_key))
            await self._redis.delete(self._dedupe_key(job))
            await self.enqueue(job)
        return drained

    async def close(self) -> None:
        await self._redis.aclose()


async def build_queue(settings: Any) -> JobQueue:
    """Redis when reachable, in-memory otherwise - with a loud warning."""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        log.warning("redis package not installed; using the in-memory queue")
        return MemoryQueue(ttl=settings.idempotency_ttl, max_attempts=settings.max_attempts)

    try:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=POLL_TIMEOUT + SOCKET_TIMEOUT_MARGIN,
            health_check_interval=30,
        )
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - any connection failure degrades
        log.warning(
            "cannot reach Redis at %s (%s); using the in-memory queue. Jobs will "
            "not survive a restart and cannot be shared across processes.",
            settings.redis_url,
            exc,
        )
        return MemoryQueue(ttl=settings.idempotency_ttl, max_attempts=settings.max_attempts)

    log.info("connected to Redis at %s", settings.redis_url)
    return RedisQueue(
        client,
        settings.queue_name,
        ttl=settings.idempotency_ttl,
        max_attempts=settings.max_attempts,
    )
