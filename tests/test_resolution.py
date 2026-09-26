"""Comment resolution (roadmap 3): what happened to what we said.

The metric here is a proxy, so most of these tests are about the proxy failing
honestly - reporting `None` rather than a perfect score, ignoring silence rather
than counting it as agreement.
"""

from __future__ import annotations

import json
from typing import Any

from app.github.client import Comparison, GitHubError, PRFile
from app.github.patch import parse_patch
from app.github.publisher import marker_for
from app.review.resolution import (
    ACKNOWLEDGED,
    ADDRESSED,
    DISPUTED,
    OPEN,
    CommentOutcome,
    ResolutionReport,
    ResolutionTracker,
    append_record,
    merge,
)

FIXED_PATCH = """@@ -6,4 +6,5 @@ def save(path, data):
 def save(path, data):
-    open(path, "w").write(data)
+    with open(path, "w") as fh:
+        fh.write(data)
"""


def comment(cid=1, line=8, review_id=100, body=None, **over) -> dict[str, Any]:
    base = {
        "id": cid,
        "pull_request_review_id": review_id,
        "path": "src/io.py",
        "line": line,
        "position": 4,
        "body": body or "🟠 **MAJOR · correctness** — Unclosed handle\n\nleaks a descriptor",
        "html_url": f"https://github.com/acme/widget/pull/7#discussion_r{cid}",
        "reactions": {},
    }
    base.update(over)
    return base


class ResolvingGitHub:
    def __init__(self, reviews, comments, compare_result=None):
        self._reviews = reviews
        self._comments = comments
        self._compare = compare_result

    async def get_json(self, path: str, **params: Any):
        if path.endswith("/reviews"):
            return self._reviews
        if path.endswith("/comments"):
            return self._comments if params.get("page", 1) == 1 else []
        return {}

    async def compare(self, owner, repo, base, head, *, max_files, max_patch_lines):
        if isinstance(self._compare, Exception):
            raise self._compare
        return self._compare or Comparison(status="ahead")


def _reviews(sha="old-sha", review_id=100):
    return [{"id": review_id, "body": f"{marker_for(sha)}\n## 🤖 AI code review"}]


def _fixed_compare():
    return Comparison(
        status="ahead",
        ahead_by=1,
        files=[
            PRFile(
                path="src/io.py",
                status="modified",
                additions=2,
                deletions=1,
                patch=parse_patch(FIXED_PATCH),
            )
        ],
    )


async def test_a_changed_line_counts_as_addressed(pr):
    gh = ResolvingGitHub(_reviews(), [comment(line=7)], _fixed_compare())
    report = await ResolutionTracker(gh).track(pr)

    assert [c.status for c in report.comments] == [ADDRESSED]
    assert "changed after the review" in report.comments[0].reason


async def test_an_untouched_line_stays_open(pr):
    gh = ResolvingGitHub(_reviews(), [comment(line=999)], _fixed_compare())
    report = await ResolutionTracker(gh).track(pr)

    assert [c.status for c in report.comments] == [OPEN]


async def test_a_thumbs_down_is_disputed(pr):
    gh = ResolvingGitHub(_reviews(), [comment(line=999, reactions={"-1": 1})], _fixed_compare())
    report = await ResolutionTracker(gh).track(pr)
    assert [c.status for c in report.comments] == [DISPUTED]


async def test_a_reply_is_acknowledged(pr):
    gh = ResolvingGitHub(
        _reviews(),
        [
            comment(cid=1, line=999),
            # A human's reply. Not ours - it belongs to no review of ours.
            {"id": 2, "in_reply_to_id": 1, "pull_request_review_id": 0, "body": "good catch"},
        ],
        _fixed_compare(),
    )
    report = await ResolutionTracker(gh).track(pr)
    assert [c.status for c in report.comments] == [ACKNOWLEDGED]


async def test_comments_from_other_reviewers_are_not_ours(pr):
    gh = ResolvingGitHub(
        _reviews(),
        [comment(cid=5, review_id=777, body="please rename this")],
        _fixed_compare(),
    )
    report = await ResolutionTracker(gh).track(pr)
    assert report.comments == []
    assert "no comments from this bot" in report.note


async def test_without_a_usable_compare_it_falls_back_to_githubs_outdated_flag(pr):
    """A force-push deletes the commit we reviewed. GitHub still knows the
    comment no longer anchors anywhere, which answers a weaker version of the
    same question."""
    gh = ResolvingGitHub(_reviews(), [comment(position=None)], GitHubError("not found: /compare"))
    report = await ResolutionTracker(gh).track(pr)

    assert [c.status for c in report.comments] == [ADDRESSED]
    assert "outdated" in report.comments[0].reason


async def test_the_severity_and_title_are_read_back_out_of_the_comment(pr):
    gh = ResolvingGitHub(_reviews(), [comment(line=7)], _fixed_compare())
    report = await ResolutionTracker(gh).track(pr)

    assert report.comments[0].severity == "major"
    assert report.comments[0].title == "Unclosed handle"


# --- the metric itself -----------------------------------------------------


def test_no_responses_reports_none_rather_than_a_perfect_score():
    """An empty ratio is not 100%. Reporting it as one is how a metric starts
    lying the moment the sample is small."""
    report = ResolutionReport(comments=[])
    assert report.precision_signal is None
    assert report.summary()["precision_signal"] is None


def test_silence_is_excluded_from_the_ratio_rather_than_counted_against_us():
    records = [{"summary": {ADDRESSED: 3, ACKNOWLEDGED: 1, DISPUTED: 1, OPEN: 40}}]
    out = merge(records)
    assert out["responded"] == 5
    assert out["precision_signal"] == 0.8


def test_merging_weights_by_comment_not_by_pr():
    """A PR carrying eight comments says more than one carrying a single
    comment; averaging the per-PR ratios would give them equal weight."""
    records = [
        {"summary": {ADDRESSED: 8, ACKNOWLEDGED: 0, DISPUTED: 0, OPEN: 0}},
        {"summary": {ADDRESSED: 0, ACKNOWLEDGED: 0, DISPUTED: 1, OPEN: 0}},
    ]
    assert merge(records)["precision_signal"] == round(8 / 9, 4)


def test_re_recording_a_pr_replaces_its_earlier_answer(tmp_path):
    """A comment that was `open` last week may be `addressed` now. Appending
    both would count it twice, with two different answers."""
    path = tmp_path / "resolution.json"
    append_record(path, ResolutionReport(pr="acme/widget#7", comments=[]))
    records = append_record(
        path,
        ResolutionReport(
            pr="acme/widget#7",
            comments=[
                CommentOutcome(
                    comment_id=1,
                    path="src/io.py",
                    line=8,
                    severity="major",
                    title="Unclosed handle",
                    status=ADDRESSED,
                    reason="src/io.py:8 changed after the review",
                )
            ],
        ),
    )
    assert len(records) == 1

    stored = json.loads(path.read_text())
    assert stored["aggregate"]["prs"] == 1
    assert stored["aggregate"][ADDRESSED] == 1, "the later answer is the one kept"


def test_an_unreadable_record_file_is_replaced_not_fatal(tmp_path):
    path = tmp_path / "resolution.json"
    path.write_text("{not json")
    records = append_record(path, ResolutionReport(pr="acme/widget#7"))
    assert len(records) == 1
