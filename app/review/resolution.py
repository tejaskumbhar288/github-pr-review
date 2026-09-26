"""Comment resolution: did the author actually act on what we said (roadmap 3).

The eval suite measures the reviewer against bugs *we* declared were there. That
is the only thing you can measure before shipping, and it has an obvious ceiling:
it scores the reviewer against its author's judgement. The pull requests it runs
on afterwards carry a second opinion, from people with no stake in the tool
looking good - whether they changed the code we pointed at.

So: for every inline comment this bot has posted on a PR, work out what happened
to it.

  addressed     the line we anchored to has changed since we commented
  acknowledged  someone replied to the thread, or reacted positively
  disputed      someone reacted negatively (👎)
  open          nothing has happened yet

**This is a proxy and it is worth being precise about how it lies.** A line can
change for reasons that have nothing to do with the comment - a rename sweeping
through, a reformat, an unrelated fix in the same function. And silence is not
rejection: most `open` comments are on PRs nobody has revisited. That is exactly
why the headline number ignores `open` entirely and reports only the ratio among
comments that drew *some* response. It is a weak signal read honestly, which is
worth more than a strong one read wrongly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..github.client import GitHubClient, GitHubError, PullRequest
from ..github.publisher import MARKER

log = logging.getLogger(__name__)

MAX_PAGES = 10

ADDRESSED = "addressed"
ACKNOWLEDGED = "acknowledged"
DISPUTED = "disputed"
OPEN = "open"

# The shape format_finding() writes: "🔴 **CRITICAL · correctness** — Title".
_HEADER_RE = re.compile(
    r"\*\*(?P<severity>[A-Z]+)\s*·\s*(?P<category>[\w-]+)\*\*\s*[—-]\s*(?P<title>.+)"
)

_POSITIVE_REACTIONS = ("+1", "heart", "hooray", "rocket")
_NEGATIVE_REACTIONS = ("-1",)


@dataclass
class CommentOutcome:
    comment_id: int
    path: str
    line: int | None
    severity: str
    title: str
    status: str
    reason: str
    url: str = ""


@dataclass
class ResolutionReport:
    pr: str = ""
    reviewed_sha: str = ""
    head_sha: str = ""
    comments: list[CommentOutcome] = field(default_factory=list)
    note: str = ""

    def count(self, status: str) -> int:
        return sum(1 for c in self.comments if c.status == status)

    @property
    def responded(self) -> int:
        """Comments that drew any reaction at all, in either direction."""
        return self.count(ADDRESSED) + self.count(ACKNOWLEDGED) + self.count(DISPUTED)

    @property
    def acted_on(self) -> int:
        return self.count(ADDRESSED) + self.count(ACKNOWLEDGED)

    @property
    def precision_signal(self) -> float | None:
        """Of the comments that got a response, the share that was acted on.

        ``None`` rather than 1.0 when nothing has drawn a response yet: an empty
        ratio is not a perfect score, and reporting it as one is how a metric
        starts lying the moment the sample is small.
        """
        return self.acted_on / self.responded if self.responded else None

    def summary(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "reviewed_sha": self.reviewed_sha[:12],
            "head_sha": self.head_sha[:12],
            "comments": len(self.comments),
            ADDRESSED: self.count(ADDRESSED),
            ACKNOWLEDGED: self.count(ACKNOWLEDGED),
            DISPUTED: self.count(DISPUTED),
            OPEN: self.count(OPEN),
            "precision_signal": (
                round(self.precision_signal, 4) if self.precision_signal is not None else None
            ),
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "comments": [asdict(c) for c in self.comments]}


class ResolutionTracker:
    def __init__(self, gh: GitHubClient):
        self._gh = gh

    async def track(self, pr: PullRequest) -> ResolutionReport:
        report = ResolutionReport(pr=pr.slug, head_sha=pr.head_sha)

        mine, every, reviewed_sha = await self._our_comments(pr)
        report.reviewed_sha = reviewed_sha or ""
        if not mine:
            report.note = "no comments from this bot on that PR"
            return report

        # Replies come from the *whole* comment list, not just ours: the reply
        # we are looking for is by definition somebody else's.
        replies = {int(c["in_reply_to_id"]) for c in every if c.get("in_reply_to_id") is not None}
        changed = await self._changed_lines(pr, reviewed_sha)

        for comment in mine:
            report.comments.append(_classify(comment, changed, replies))
        return report

    async def _our_comments(
        self, pr: PullRequest
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        """Every inline comment on the PR, split into ours and everyone's.

        Ours are identified through the reviews that carry our marker, not by
        author login: the same review can be posted by a PAT today and a GitHub
        App tomorrow, and a login check would quietly stop recognising the bot's
        own history the day the deployment changed.
        """
        try:
            reviews = await self._gh.get_json(
                f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews", per_page=100
            )
        except GitHubError as exc:
            log.warning("could not list reviews: %s", exc)
            return [], [], None

        our_reviews = {
            int(r["id"]): _marked_sha(r.get("body") or "")
            for r in reviews or []
            if MARKER in (r.get("body") or "")
        }
        if not our_reviews:
            return [], [], None

        every: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            batch = await self._gh.get_json(
                f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments",
                per_page=100,
                page=page,
            )
            if not batch:
                break
            every.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        mine = [c for c in every if int(c.get("pull_request_review_id") or 0) in our_reviews]
        # The commit the most recent of our reviews described; the baseline the
        # "has this line changed" question is asked against.
        latest = max(our_reviews, default=0)
        return mine, every, our_reviews.get(latest)

    async def _changed_lines(
        self, pr: PullRequest, reviewed_sha: str | None
    ) -> dict[str, set[int]] | None:
        """Lines touched between the commit we reviewed and the current head.

        ``None`` means the question could not be answered - a force-push, a
        deleted commit - and the caller must then fall back to GitHub's own
        `position is null` flag rather than guessing.
        """
        if not reviewed_sha or reviewed_sha == pr.head_sha:
            return {}
        try:
            comparison = await self._gh.compare(
                pr.owner, pr.repo, reviewed_sha, pr.head_sha, max_files=200, max_patch_lines=5000
            )
        except GitHubError as exc:
            log.info("cannot compare %s...%s: %s", reviewed_sha[:8], pr.head_sha[:8], exc)
            return None
        return {f.path: set(f.patch.commentable) for f in comparison.files}


def _classify(
    comment: dict[str, Any], changed: dict[str, set[int]] | None, replies: set[int]
) -> CommentOutcome:
    body = str(comment.get("body") or "")
    header = _HEADER_RE.search(body)
    path = str(comment.get("path") or "")
    line = comment.get("line")
    line = int(line) if isinstance(line, int) else None
    comment_id = int(comment.get("id") or 0)
    reactions = comment.get("reactions") or {}

    outcome = CommentOutcome(
        comment_id=comment_id,
        path=path,
        line=line,
        severity=(header.group("severity").lower() if header else "unknown"),
        title=(header.group("title").strip() if header else body.splitlines()[0][:80]),
        status=OPEN,
        reason="no activity on this comment",
        url=str(comment.get("html_url") or ""),
    )

    # Code first: a change to the line we pointed at is the strongest evidence
    # available, and it is the only one that does not depend on someone
    # remembering to click something.
    if changed is None:
        # No usable compare. GitHub nulls `position` once a comment's anchor no
        # longer exists in the diff, which answers a weaker version of the same
        # question, so use it rather than reporting nothing.
        if comment.get("position") is None:
            outcome.status = ADDRESSED
            outcome.reason = "GitHub marked the comment outdated"
            return outcome
    elif line is not None and line in changed.get(path, set()):
        outcome.status = ADDRESSED
        outcome.reason = f"{path}:{line} changed after the review"
        return outcome

    if any(int(reactions.get(r) or 0) > 0 for r in _NEGATIVE_REACTIONS):
        outcome.status = DISPUTED
        outcome.reason = "a reviewer reacted 👎"
        return outcome

    if comment_id in replies:
        outcome.status = ACKNOWLEDGED
        outcome.reason = "someone replied in the thread"
        return outcome

    if any(int(reactions.get(r) or 0) > 0 for r in _POSITIVE_REACTIONS):
        outcome.status = ACKNOWLEDGED
        outcome.reason = "a reviewer reacted positively"
        return outcome

    return outcome


def _marked_sha(body: str) -> str | None:
    match = re.search(rf"<!--\s*{re.escape(MARKER)}:([^\s>]{{4,64}})\s*-->", body or "")
    return match.group(1) if match else None


# --- aggregation across PRs -----------------------------------------------


def merge(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll several per-PR reports into one signal.

    Summed rather than averaged: a PR carrying eight comments says more about
    the reviewer than one carrying a single comment, and averaging the per-PR
    ratios would give them equal weight.
    """
    totals = dict.fromkeys((ADDRESSED, ACKNOWLEDGED, DISPUTED, OPEN), 0)
    for record in records:
        summary = record.get("summary", record)
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
    responded = totals[ADDRESSED] + totals[ACKNOWLEDGED] + totals[DISPUTED]
    acted = totals[ADDRESSED] + totals[ACKNOWLEDGED]
    return {
        "prs": len(records),
        "comments": sum(totals.values()),
        **totals,
        "responded": responded,
        "precision_signal": round(acted / responded, 4) if responded else None,
    }


def append_record(path: Path, report: ResolutionReport) -> list[dict[str, Any]]:
    """Store this PR's report, replacing any earlier one for the same PR.

    Replaced rather than appended because a PR's outcome keeps evolving - a
    comment that was `open` last week may be `addressed` now - and keeping both
    would count the same comment twice with two different answers.
    """
    records: list[dict[str, Any]] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
            records = loaded.get("records", []) if isinstance(loaded, dict) else list(loaded)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("ignoring unreadable %s: %s", path, exc)

    records = [r for r in records if (r.get("summary") or {}).get("pr") != report.pr]
    records.append(report.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aggregate": merge(records), "records": records}, indent=2) + "\n")
    return records


# --- entrypoint -----------------------------------------------------------


def render(report: ResolutionReport) -> str:
    s = report.summary()
    lines = [
        "",
        f"Resolution for {s['pr']} (reviewed {s['reviewed_sha']}, now at {s['head_sha']})",
        "",
    ]
    if not report.comments:
        lines.append(s["note"] or "nothing to report")
        return "\n".join(lines)

    for c in report.comments:
        anchor = f"{c.path}:{c.line}" if c.line else c.path
        lines.append(f"  {c.status:<13} {anchor:<40} {c.title[:50]}")
        lines.append(f"  {'':<13} {c.reason}")

    signal = s["precision_signal"]
    lines += [
        "",
        f"{s['comments']} comment(s): {s[ADDRESSED]} addressed, "
        f"{s[ACKNOWLEDGED]} acknowledged, {s[DISPUTED]} disputed, {s[OPEN]} open",
    ]
    if signal is None:
        lines.append("precision signal: n/a - no comment has drawn a response yet")
    else:
        lines.append(
            f"precision signal: {signal:.0%} of the {report.responded} comment(s) that "
            f"drew a response were acted on"
        )
    return "\n".join(lines)


async def _main(argv: list[str] | None = None) -> int:
    import argparse

    from ..config import get_settings
    from ..github.client import parse_pr_url
    from ..obs.logging import setup_logging

    parser = argparse.ArgumentParser(
        prog="ai-review-resolution",
        description="Report what happened to the review comments this bot posted",
    )
    parser.add_argument("url", nargs="?", help="GitHub pull request URL")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--record",
        metavar="PATH",
        nargs="?",
        const="evals/resolution.json",
        help="store the report so it accumulates across PRs (default: evals/resolution.json)",
    )
    parser.add_argument(
        "--summary",
        metavar="PATH",
        nargs="?",
        const="evals/resolution.json",
        help="print the aggregate across everything recorded so far, and exit",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.summary:
        path = Path(args.summary)
        if not path.is_file():
            print(f"no records yet at {path}")
            return 0
        loaded = json.loads(path.read_text())
        records = loaded.get("records", []) if isinstance(loaded, dict) else list(loaded)
        print(json.dumps(merge(records), indent=2))
        return 0

    if not args.url:
        parser.error("a pull request URL is required unless --summary is given")

    owner, repo, number = parse_pr_url(args.url)
    async with GitHubClient(settings.github_token, settings.github_api) as gh:
        pr = await gh.fetch_pr(
            owner,
            repo,
            number,
            max_files=settings.max_files,
            max_patch_lines=settings.max_patch_lines,
        )
        report = await ResolutionTracker(gh).track(pr)

    if args.record:
        records = append_record(Path(args.record), report)
        log.info("recorded %s (%d PR(s) tracked)", report.pr, len(records))

    print(json.dumps(report.to_dict(), indent=2) if args.json else render(report))
    return 0


def main() -> None:  # pragma: no cover - process entrypoint
    import asyncio
    import sys

    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except (GitHubError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":  # pragma: no cover
    main()
