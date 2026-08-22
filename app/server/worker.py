"""Async review worker (roadmap 3).

Consumes jobs the webhook enqueued and does the slow part: fetch, reason,
publish. Runs as its own process so the webhook endpoint can keep answering in
milliseconds no matter how long a review takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from ..config import ConfigError, Settings, get_settings
from ..github.auth import GitHubAppAuth, GitHubAuthError, load_private_key
from ..obs.logging import setup_logging
from ..obs.tracing import Tracer, build_tracer
from ..pipeline import ReviewSession
from .queue import POLL_TIMEOUT, JobQueue, RedisQueue, ReviewJob, build_queue

log = logging.getLogger(__name__)


class ReviewWorker:
    def __init__(
        self,
        settings: Settings,
        queue: JobQueue,
        *,
        auth: GitHubAppAuth | None = None,
        tracer: Tracer | None = None,
    ):
        self._settings = settings
        self._queue = queue
        self._auth = auth
        self._tracer = tracer or build_tracer(settings)
        self._stopping = asyncio.Event()
        self.processed = 0
        self.failed = 0
        self.dead_lettered = 0

    def stop(self) -> None:
        self._stopping.set()

    async def run(self, concurrency: int | None = None) -> None:
        concurrency = concurrency or self._settings.worker_concurrency
        if isinstance(self._queue, RedisQueue):
            await self._queue.requeue_stale()

        log.info("worker started with %d consumer(s)", concurrency)
        consumers = [asyncio.create_task(self._consume(i)) for i in range(concurrency)]
        try:
            await self._stopping.wait()
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            self._tracer.shutdown()
            log.info(
                "worker stopped (processed=%d failed=%d dead_lettered=%d)",
                self.processed,
                self.failed,
                self.dead_lettered,
            )

    async def _consume(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                job = await self._queue.dequeue(timeout=POLL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken queue must not kill the loop
                log.error("consumer %d: dequeue failed: %s", index, exc)
                await asyncio.sleep(1.0)
                continue

            if job is None:
                continue
            await self.handle(job)

    async def handle(self, job: ReviewJob) -> bool:
        started = time.perf_counter()
        log.info("reviewing %s (queued %.1fs ago)", job.dedupe_key, started - job.enqueued_at)
        try:
            token = await self._token_for(job)
            async with ReviewSession(self._settings, token=token, tracer=self._tracer) as session:
                outcome = await session.run(
                    job.owner, job.repo, job.number, post=True, skip_if_reviewed=True
                )
        except asyncio.CancelledError:
            # Release the reservation so the job is retried after a restart.
            await self._queue.fail(job, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - one bad PR must not kill the worker
            self.failed += 1
            log.exception("review failed for %s", job.dedupe_key)
            if await self._queue.fail(job, f"{type(exc).__name__}: {exc}"):
                self.dead_lettered += 1
            return False

        await self._queue.complete(job)
        self.processed += 1
        published = outcome.published
        log.info(
            "done %s in %.1fs: %d finding(s), %s",
            job.dedupe_key,
            time.perf_counter() - started,
            len(outcome.result.findings),
            (published.url or published.skipped_reason) if published else "not posted",
        )
        return True

    async def _token_for(self, job: ReviewJob) -> str | None:
        if self._auth is None or job.installation_id is None:
            return self._settings.github_token
        try:
            return await self._auth.installation_token(job.installation_id)
        except GitHubAuthError as exc:
            log.error("could not mint an installation token: %s", exc)
            raise


def build_auth(settings: Settings) -> GitHubAppAuth | None:
    if not settings.github_app_id:
        return None
    try:
        key = load_private_key(settings.github_private_key, settings.github_private_key_path)
    except GitHubAuthError as exc:
        log.warning("GitHub App auth unavailable (%s); falling back to GITHUB_TOKEN", exc)
        return None
    return GitHubAppAuth(settings.github_app_id, key, settings.github_api)


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    settings.validate()

    queue = await build_queue(settings)
    auth = build_auth(settings)
    if auth is None and not settings.github_token:
        log.warning(
            "no GitHub App key and no GITHUB_TOKEN: reviews will be read-only and "
            "publishing will fail"
        )

    worker = ReviewWorker(settings, queue, auth=auth)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run()
    finally:
        await queue.close()
        if auth is not None:
            await auth.close()


def main() -> None:  # pragma: no cover - process entrypoint
    try:
        asyncio.run(run_worker())
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
