"""Publish a review back to the pull request (roadmap 1).

Turning findings into a posted review is where anchoring stops being academic:
GitHub 422s the *entire* request if a single inline comment points at a line
outside the diff. Validation in the engine makes that rare; this module makes
it non-fatal, by degrading to a summary-only review rather than losing the
whole result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..review.analysis import render_known_issues
from .client import GitHubClient, GitHubError

if TYPE_CHECKING:  # pragma: no cover
    from ..review.engine import Finding, ReviewResult
    from .client import PullRequest

log = logging.getLogger(__name__)

# A hidden marker so we can recognise our own reviews under either a PAT or a
# GitHub App installation, without needing to resolve the current identity.
MARKER = "ai-code-review"

SEVERITY_EMOJI = {"critical": "🔴", "major": "🟠", "minor": "🔵"}
MAX_BODY = 60_000
MAX_COMMENT = 60_000
MAX_INLINE_COMMENTS = 50


@dataclass
class PriorReview:
    """One review we posted previously, and the commit it described."""

    sha: str
    review_id: int = 0
    submitted_at: str = ""


@dataclass
class PublishResult:
    posted: bool
    review_id: int | None = None
    url: str | None = None
    inline_comments: int = 0
    degraded: bool = False
    """True when inline anchoring failed and we fell back to a summary review."""
    skipped_reason: str | None = None


def marker_for(head_sha: str) -> str:
    return f"<!-- {MARKER}:{head_sha} -->"


# Deliberately not `[0-9a-f]{40}`: the marker holds whatever ref we reviewed,
# and tightening this to real SHAs would quietly stop recognising our own
# reviews the first time something hands us a short SHA or a tag.
_MARKER_RE = re.compile(rf"<!--\s*{re.escape(MARKER)}:([^\s>]{{4,64}})\s*-->")


def _marked_sha(body: str) -> str | None:
    """Pull the reviewed commit out of a review body, if we wrote it."""
    match = _MARKER_RE.search(body or "")
    return match.group(1) if match else None


def format_finding(f: Finding) -> str:
    emoji = SEVERITY_EMOJI.get(f.severity, "🔵")
    parts = [f"{emoji} **{f.severity.upper()} · {f.category}** — {f.title}"]
    if f.detail:
        parts.append(f"\n{f.detail}")
    if f.suggestion:
        lang = _lang_for(f.file)
        # GitHub renders ```suggestion blocks as one-click commits, but only when
        # the replacement lines up with the anchored line. We can't guarantee
        # that for multi-line advice, so present it as a normal code block.
        parts.append(f"\n```{lang}\n{f.suggestion.strip()}\n```")
    return "\n".join(parts)[:MAX_COMMENT]


def build_body(pr: PullRequest, result: ReviewResult, *, degraded: bool = False) -> str:
    counts = result.counts
    lines = [
        marker_for(pr.head_sha),
        "## 🤖 AI code review",
        "",
    ]
    # Scope goes above the summary, not in the footer. A reader who does not
    # know the review covered two commits rather than the whole PR will read
    # "no blocking issues found" as a verdict on the whole PR.
    if pr.is_incremental:
        lines += [
            f"_Incremental review: the {pr.new_commits} commit(s) pushed since "
            f"`{pr.incremental_base[:7]}`. Earlier commits were reviewed above._",
            "",
        ]
    lines += [
        result.summary or "_No summary returned._",
        "",
    ]

    if result.findings:
        lines.append(
            f"**{counts['critical']} critical · {counts['major']} major · {counts['minor']} minor**"
        )
    else:
        lines.append("**No blocking issues found.**")

    if degraded and result.findings:
        lines += ["", "---", "", "_Inline anchoring failed, so findings are listed here._", ""]
        for f in result.findings:
            lines.append(f"**`{f.file}:{f.line}`** — {format_finding(f)}")
            lines.append("")

    # The pre-pass tells the model not to repeat what the linters found. That
    # only saves attention if the linter findings are still reported *somewhere*
    # - otherwise telling the model to skip them silently swallows real issues.
    if result.known_issues:
        lines += [
            "",
            "---",
            "",
            f"<details><summary>{len(result.known_issues)} issue(s) from static analysis</summary>",
            "",
            "```",
            render_known_issues(result.known_issues),
            "```",
            "",
            "</details>",
        ]

    footer = []
    if result.dropped:
        footer.append(f"{len(result.dropped)} finding(s) dropped as unanchorable")
    if pr.truncated_files:
        footer.append(f"{pr.truncated_files} file(s) excluded from review")
    if result.metrics.model:
        footer.append(f"model `{result.metrics.model}`")
    if footer:
        lines += ["", "---", f"<sub>{' · '.join(footer)}</sub>"]

    return "\n".join(lines)[:MAX_BODY]


class ReviewPublisher:
    def __init__(self, gh: GitHubClient, *, event: str = "COMMENT"):
        self._gh = gh
        self._event = event

    async def review_history(self, pr: PullRequest) -> list[PriorReview]:
        """Our own past reviews of this PR, oldest first.

        One request answers two questions - "did we already review this commit?"
        and "what was the last commit we did review?" - so they share a call
        rather than each paying for one. The marker carries the SHA, which is
        why the answer survives a token swap or a move from a PAT to an App:
        it never has to work out which account posted the review.
        """
        try:
            reviews = await self._gh.get_json(
                f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews", per_page=100
            )
        except GitHubError as exc:
            log.warning("could not list existing reviews: %s", exc)
            return []

        history: list[PriorReview] = []
        for r in reviews or []:
            sha = _marked_sha(r.get("body") or "")
            if sha is None:
                continue
            history.append(
                PriorReview(
                    sha=sha,
                    review_id=int(r.get("id") or 0),
                    submitted_at=str(r.get("submitted_at") or ""),
                )
            )
        return history

    async def already_reviewed(self, pr: PullRequest) -> bool:
        """Has this exact head_sha already been reviewed by us?

        Cheap protection against duplicate reviews when a webhook is redelivered
        or the CLI is re-run; the queue's idempotency key is the primary guard.
        """
        return any(r.sha == pr.head_sha for r in await self.review_history(pr))

    async def last_reviewed_sha(self, pr: PullRequest) -> str | None:
        """The most recent commit of this PR we have already reviewed, if any.

        Ordered by GitHub's own submission order rather than by our marker,
        because the marker records which commit a review was *about* and gives
        no ordering of its own. Reviews at the current head are ignored: the
        caller wants a starting point for what is new, and "new since head" is
        empty by definition.
        """
        prior = [r for r in await self.review_history(pr) if r.sha != pr.head_sha]
        return prior[-1].sha if prior else None

    async def publish(
        self,
        pr: PullRequest,
        result: ReviewResult,
        *,
        skip_if_reviewed: bool = True,
    ) -> PublishResult:
        if skip_if_reviewed and await self.already_reviewed(pr):
            log.info("review for %s already exists, skipping", pr.idempotency_key)
            return PublishResult(posted=False, skipped_reason="already reviewed at this commit")

        comments = self._build_comments(result)
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
        payload: dict[str, Any] = {
            "commit_id": pr.head_sha,
            "body": build_body(pr, result),
            "event": self._resolve_event(result),
            "comments": comments,
        }

        try:
            data = await self._gh.post_json(path, payload)
        except GitHubError as exc:
            if not comments or not _is_anchor_rejection(exc):
                raise
            log.warning("inline anchoring rejected by GitHub, degrading: %s", exc)
            payload["body"] = build_body(pr, result, degraded=True)
            payload["comments"] = []
            data = await self._gh.post_json(path, payload)
            return PublishResult(
                posted=True,
                review_id=data.get("id"),
                url=data.get("html_url"),
                inline_comments=0,
                degraded=True,
            )

        return PublishResult(
            posted=True,
            review_id=data.get("id"),
            url=data.get("html_url"),
            inline_comments=len(comments),
        )

    def _resolve_event(self, result: ReviewResult) -> str:
        # Never "APPROVE" on a PR we found problems in, whatever the config says.
        if self._event == "APPROVE" and result.findings:
            return "COMMENT"
        # Only escalate to REQUEST_CHANGES when something is genuinely critical.
        if self._event == "REQUEST_CHANGES" and not result.counts["critical"]:
            return "COMMENT"
        return self._event

    def _build_comments(self, result: ReviewResult) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for f in result.findings[:MAX_INLINE_COMMENTS]:
            comments.append(
                {
                    "path": f.file,
                    "line": f.line,
                    "side": "RIGHT",
                    "body": format_finding(f),
                }
            )
        return comments


def _is_anchor_rejection(exc: GitHubError) -> bool:
    text = str(exc).lower()
    return "422" in text and (
        "line" in text or "position" in text or "diff" in text or "path" in text
    )


_LANGS = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "rb": "ruby",
    "php": "php",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cs": "csharp",
    "kt": "kotlin",
    "swift": "swift",
    "sh": "bash",
    "sql": "sql",
    "yml": "yaml",
    "yaml": "yaml",
    "json": "json",
    "tf": "hcl",
    "scala": "scala",
}


def _lang_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _LANGS.get(ext, "")
