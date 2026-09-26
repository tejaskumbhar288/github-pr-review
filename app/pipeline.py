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
from .review.incremental import IncrementalOutcome, narrow_to_new_commits

log = logging.getLogger(__name__)


@dataclass
class ReviewOutcome:
    result: ReviewResult
    published: PublishResult | None = None
    incremental: str = IncrementalOutcome.FULL
    """Which of the incremental outcomes applied; see review.incremental."""


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
        incremental: bool | None = None,
    ) -> ReviewOutcome:
        pr = await self.gh.fetch_pr(
            owner,
            repo,
            number,
            max_files=self.settings.max_files,
            max_patch_lines=self.settings.max_patch_lines,
        )
        return await self.run_pr(
            pr, post=post, skip_if_reviewed=skip_if_reviewed, incremental=incremental
        )

    async def run_pr(
        self,
        pr: PullRequest,
        *,
        post: bool | None = None,
        skip_if_reviewed: bool = True,
        incremental: bool | None = None,
    ) -> ReviewOutcome:
        should_post = self.settings.post_reviews if post is None else post
        use_incremental = self.settings.incremental_review if incremental is None else incremental

        # One call answers both questions: whether this exact commit is already
        # reviewed, and which commit we last reviewed. Asking separately paid
        # for the same request twice.
        history = await self.publisher.review_history(pr) if use_incremental else []

        # Check for an existing review *before* reasoning, not after. The
        # duplicate check used to run at publish time, so a redelivered webhook
        # burned a full review - tens of thousands of tokens and ~45s - only to
        # discard the result. The queue's reservation covers the common case;
        # this covers a queue miss, a manual re-run, and a second worker.
        if should_post and skip_if_reviewed:
            seen = (
                any(r.sha == pr.head_sha for r in history)
                if use_incremental
                else await self.publisher.already_reviewed(pr)
            )
            if seen:
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

        mode = IncrementalOutcome.FULL
        if use_incremental:
            prior = [r for r in history if r.sha != pr.head_sha]
            if prior:
                pr, mode = await narrow_to_new_commits(self.gh, pr, prior[-1].sha, self.settings)

        if mode == IncrementalOutcome.NOTHING_NEW:
            # Deliberately not posted. A second review saying "nothing new" is
            # noise on the PR, and it would consume a model request to produce.
            log.info("%s: nothing new to review since %s", pr.slug, pr.incremental_base)
            return ReviewOutcome(
                result=ReviewResult(
                    pr=pr,
                    summary=(
                        f"No reviewable changes in the {pr.new_commits} commit(s) pushed "
                        f"since the last review."
                    ),
                    metrics=ReviewMetrics(pr=pr.slug, head_sha=pr.head_sha),
                ),
                published=PublishResult(
                    posted=False, skipped_reason="no reviewable changes since the last review"
                )
                if should_post
                else None,
                incremental=mode,
            )

        result = await self.engine.review_pr(pr)
        if not should_post:
            return ReviewOutcome(result=result, incremental=mode)

        # skip_if_reviewed is already settled above when the history was
        # fetched; re-checking here would list the reviews a second time.
        published = await self.publisher.publish(pr, result, skip_if_reviewed=False)
        return ReviewOutcome(result=result, published=published, incremental=mode)

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
