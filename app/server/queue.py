"""Job queue and idempotency (roadmap 3).

GitHub redelivers webhooks - on its own retry schedule, and on demand from the
UI - so the same push can arrive several times. The dedupe key is
``(owner/repo#number, head_sha)``: a redelivery for a commit already reviewed is
dropped, but a *new* commit on the same PR is a genuinely new job.

Reservations are released when a job fails, so a transient LLM outage doesn't
permanently poison a PR. Jobs move through a processing list rather than a bare
BRPOP, so a worker crash doesn't silently lose the review.
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


class JobQueue(Protocol):
    async def enqueue(self, job: ReviewJob) -> bool: ...
    async def dequeue(self, timeout: float = POLL_TIMEOUT) -> ReviewJob | None: ...
    async def complete(self, job: ReviewJob) -> None: ...
    async def fail(self, job: ReviewJob, error: str) -> None: ...
    async def depth(self) -> int: ...
    async def close(self) -> None: ...


class MemoryQueue:
    """In-process queue. Correct for a single process; the default when Redis
    is unavailable, so `python -m app.server.worker` works with no infra."""

    def __init__(self, ttl: int = 86_400):
        self._queue: asyncio.Queue[ReviewJob] = asyncio.Queue()
        self._reservations: dict[str, float] = {}
        self._ttl = ttl
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

    async def fail(self, job: ReviewJob, error: str) -> None:
        self._reservations.pop(job.dedupe_key, None)

    async def depth(self) -> int:
        return self._queue.qsize()

    async def close(self) -> None:
        return None


class RedisQueue:
    """Redis-backed queue with crash-safe handoff via a processing list."""

    def __init__(self, redis: Any, queue_name: str = "reviews", ttl: int = 86_400):
        self._redis = redis
        self._key = f"queue:{queue_name}"
        self._processing = f"queue:{queue_name}:processing"
        self._ttl = ttl
        self.name = "redis"

    def _dedupe_key(self, job: ReviewJob) -> str:
        return f"review:seen:{job.dedupe_key}"

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

    async def fail(self, job: ReviewJob, error: str) -> None:
        await self._redis.lrem(self._processing, 1, job.to_json())
        # Release so a redelivery (or a manual retry) can pick it up again.
        await self._redis.delete(self._dedupe_key(job))
        log.warning("job %s failed: %s", job.dedupe_key, error)

    async def requeue_stale(self) -> int:
        """Move anything left in the processing list back onto the queue.

        Called at worker startup: whatever is sitting there belongs to a worker
        that died mid-review.
        """
        moved = 0
        while await self._redis.rpoplpush(self._processing, self._key):
            moved += 1
            if moved > 1000:  # pathological; stop rather than spin
                break
        if moved:
            log.warning("requeued %d job(s) orphaned by a previous worker", moved)
        return moved

    async def depth(self) -> int:
        return int(await self._redis.llen(self._key))

    async def close(self) -> None:
        await self._redis.aclose()


async def build_queue(settings: Any) -> JobQueue:
    """Redis when reachable, in-memory otherwise - with a loud warning."""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        log.warning("redis package not installed; using the in-memory queue")
        return MemoryQueue(ttl=settings.idempotency_ttl)

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
        return MemoryQueue(ttl=settings.idempotency_ttl)

    log.info("connected to Redis at %s", settings.redis_url)
    return RedisQueue(client, settings.queue_name, ttl=settings.idempotency_ttl)
