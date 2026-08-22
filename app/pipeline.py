"""Wiring shared by the CLI and the worker.

Both entrypoints need the same object graph - GitHub client, LLM provider,
engine, publisher - and both need it torn down cleanly. Keeping that in one
place stops the two paths from drifting, which is how a webhook ends up
behaving differently from the CLI you tested with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .github.client import GitHubClient, PullRequest
from .github.publisher import PublishResult, ReviewPublisher
from .llm.base import LLMProvider
from .llm.factory import build_provider
from .obs.metrics import ReviewMetrics
from .obs.tracing import Tracer, build_tracer
from .review.engine import ReviewEngine, ReviewResult

log = logging.getLogger(__name__)


@dataclass
class ReviewOutcome:
    result: ReviewResult
    published: PublishResult | None = None


class ReviewSession:
    """One configured review pipeline. Use as an async context manager."""

    def __init__(
        self,
        settings: Settings,
        *,
        token: str | None = None,
        tracer: Tracer | None = None,
        gh: GitHubClient | None = None,
        llm: LLMProvider | None = None,
    ):
        self.settings = settings
        self._owns_gh = gh is None
        self._owns_llm = llm is None
        self.gh = gh or GitHubClient(
            token if token is not None else settings.github_token,
            settings.github_api,
        )
        self.llm = llm or build_provider(settings)
        self.tracer = tracer or build_tracer(settings)
        self.engine = ReviewEngine(self.gh, self.llm, settings, self.tracer)
        self.publisher = ReviewPublisher(self.gh, event=settings.review_event)

    async def run(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        post: bool | None = None,
        skip_if_reviewed: bool = True,
    ) -> ReviewOutcome:
        pr = await self.gh.fetch_pr(
            owner,
            repo,
            number,
            max_files=self.settings.max_files,
            max_patch_lines=self.settings.max_patch_lines,
        )
        return await self.run_pr(pr, post=post, skip_if_reviewed=skip_if_reviewed)

    async def run_pr(
        self,
        pr: PullRequest,
        *,
        post: bool | None = None,
        skip_if_reviewed: bool = True,
    ) -> ReviewOutcome:
        should_post = self.settings.post_reviews if post is None else post

        # Check for an existing review *before* reasoning, not after. The
        # duplicate check used to run at publish time, so a redelivered webhook
        # burned a full review - tens of thousands of tokens and ~45s - only to
        # discard the result. The queue's reservation covers the common case;
        # this covers a queue miss, a manual re-run, and a second worker.
        if should_post and skip_if_reviewed and await self.publisher.already_reviewed(pr):
            log.info("%s already reviewed at this commit; skipping", pr.idempotency_key)
            return ReviewOutcome(
                result=ReviewResult(
                    pr=pr,
                    summary="Already reviewed at this commit.",
                    metrics=ReviewMetrics(pr=pr.slug, head_sha=pr.head_sha),
                ),
                published=PublishResult(
                    posted=False, skipped_reason="already reviewed at this commit"
                ),
            )

        result = await self.engine.review_pr(pr)
        if not should_post:
            return ReviewOutcome(result=result)

        published = await self.publisher.publish(pr, result, skip_if_reviewed=skip_if_reviewed)
        return ReviewOutcome(result=result, published=published)

    async def close(self) -> None:
        if self._owns_gh:
            await self.gh.close()
        if self._owns_llm:
            await self.llm.close()
        self.tracer.flush()

    async def __aenter__(self) -> ReviewSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
