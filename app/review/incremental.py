"""Incremental review: look only at what is new since we last looked (roadmap 1).

A pull request gets reviewed on every push. Without this, the second push
re-reviews the first push's code - which costs a full model request against a
20/day free tier, and produces a review whose findings the author has already
read and either fixed or decided against. Re-raising them is how a bot teaches
people to stop reading it.

The narrowing is deliberately conservative, because the failure mode is silent:
a review that *looks* complete while covering half the change is worse than one
that plainly re-reads everything. So the increment is used only when GitHub says
the head is a fast-forward of the commit we reviewed last. After a rebase or a
force-push the two have diverged, the diff between them mixes new work with
rewritten history, and the whole PR is re-read instead.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ..github.client import GitHubClient, GitHubError, PullRequest

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = logging.getLogger(__name__)


class IncrementalOutcome:
    """Why the narrowing did or did not happen. Reported, never inferred."""

    FULL = "full"
    """No prior review, or none usable - the whole PR was read."""
    NARROWED = "narrowed"
    """Only the commits added since the last review were read."""
    NOTHING_NEW = "nothing_new"
    """The head differs from the reviewed commit but no reviewable file does."""
    DIVERGED = "diverged"
    """History was rewritten since the last review; fell back to a full read."""
    UNAVAILABLE = "unavailable"
    """The compare call failed; fell back to a full read."""


async def narrow_to_new_commits(
    gh: GitHubClient,
    pr: PullRequest,
    since_sha: str,
    settings: Settings,
) -> tuple[PullRequest, str]:
    """Return the PR restricted to commits added after ``since_sha``.

    Returns the *original* PR unchanged, with a reason, whenever narrowing is
    not safe. Every caller therefore gets a reviewable PR back; none of them
    need to know how the decision was made.
    """
    if not since_sha or since_sha == pr.head_sha:
        return pr, IncrementalOutcome.FULL

    try:
        comparison = await gh.compare(
            pr.owner,
            pr.repo,
            since_sha,
            pr.head_sha,
            max_files=settings.max_files,
            max_patch_lines=settings.max_patch_lines,
        )
    except GitHubError as exc:
        # A missing base commit is the ordinary case here, not an anomaly: the
        # branch was force-pushed and the commit we reviewed no longer exists.
        log.info(
            "cannot compare %s...%s (%s); reviewing the full diff",
            since_sha[:8],
            pr.head_sha[:8],
            exc,
        )
        return pr, IncrementalOutcome.UNAVAILABLE

    if not comparison.is_fast_forward:
        log.info(
            "%s has %s since %s (ahead %d, behind %d); reviewing the full diff",
            pr.slug,
            comparison.status,
            since_sha[:8],
            comparison.ahead_by,
            comparison.behind_by,
        )
        return pr, IncrementalOutcome.DIVERGED

    reviewable = [f for f in comparison.files if not f.patch.is_empty]
    if not reviewable:
        # New commits that touch nothing we would review - a merge from main, a
        # lockfile bump, a docs-only push. There is genuinely nothing to say,
        # and saying it again would just be the previous review reposted.
        log.info(
            "%s: %d new commit(s) since %s touch no reviewable file",
            pr.slug,
            comparison.ahead_by,
            since_sha[:8],
        )
        return replace(
            pr, files=[], incremental_base=since_sha, new_commits=comparison.ahead_by
        ), IncrementalOutcome.NOTHING_NEW

    log.info(
        "%s: reviewing %d file(s) from %d new commit(s) since %s",
        pr.slug,
        len(comparison.files),
        comparison.ahead_by,
        since_sha[:8],
    )
    return (
        replace(
            pr,
            files=comparison.files,
            truncated_files=comparison.truncated_files,
            incremental_base=since_sha,
            new_commits=comparison.ahead_by,
        ),
        IncrementalOutcome.NARROWED,
    )
