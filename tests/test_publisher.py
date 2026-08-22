from __future__ import annotations

import pytest

from app.github.client import GitHubError
from app.github.publisher import ReviewPublisher, build_body, marker_for
from app.obs.metrics import ReviewMetrics
from app.review.engine import Finding, ReviewResult
from tests.conftest import FakeGitHub


def make_result(pr, findings=()):
    return ReviewResult(
        pr=pr,
        summary="Adds a save() helper.",
        findings=list(findings),
        metrics=ReviewMetrics(model="fake-1"),
    )


def finding(**over):
    base = {
        "file": "src/io.py",
        "line": 8,
        "severity": "major",
        "category": "correctness",
        "title": "Unclosed handle",
        "detail": "leaks a descriptor",
        "suggestion": "with open(p) as fh:\n    fh.write(d)",
    }
    base.update(over)
    return Finding(**base)


async def test_publishes_inline_comments_anchored_to_the_diff(pr):
    gh = FakeGitHub(pr)
    out = await ReviewPublisher(gh).publish(pr, make_result(pr, [finding()]))

    assert out.posted and out.inline_comments == 1 and not out.degraded
    path, payload = gh.posted[0]
    assert path == "/repos/acme/widget/pulls/7/reviews"
    assert payload["commit_id"] == "head456"
    assert payload["event"] == "COMMENT"
    comment = payload["comments"][0]
    assert comment == {
        "path": "src/io.py",
        "line": 8,
        "side": "RIGHT",
        "body": comment["body"],
    }
    assert "Unclosed handle" in comment["body"]
    assert "```python" in comment["body"]


async def test_body_carries_the_marker_and_counts(pr):
    body = build_body(pr, make_result(pr, [finding(severity="critical")]))
    assert marker_for("head456") in body
    assert "1 critical" in body


async def test_clean_review_says_so(pr):
    gh = FakeGitHub(pr)
    await ReviewPublisher(gh).publish(pr, make_result(pr))
    assert "No blocking issues found." in gh.posted[0][1]["body"]


async def test_422_degrades_to_a_summary_review_instead_of_losing_everything(pr):
    gh = FakeGitHub(pr)
    gh.raise_on_post = GitHubError("GitHub 422 for /reviews: line must be part of the diff")

    out = await ReviewPublisher(gh).publish(pr, make_result(pr, [finding()]))

    assert out.posted and out.degraded and out.inline_comments == 0
    assert len(gh.posted) == 1
    body = gh.posted[0][1]["body"]
    assert "Inline anchoring failed" in body
    assert "Unclosed handle" in body


async def test_non_anchor_errors_are_not_swallowed(pr):
    gh = FakeGitHub(pr)
    gh.raise_on_post = GitHubError("GitHub 401 for /reviews: bad credentials")
    with pytest.raises(GitHubError, match="401"):
        await ReviewPublisher(gh).publish(pr, make_result(pr, [finding()]))


async def test_duplicate_review_for_the_same_commit_is_skipped(pr):
    gh = FakeGitHub(pr)
    gh.reviews = [{"body": f"{marker_for('head456')}\nold review"}]

    out = await ReviewPublisher(gh).publish(pr, make_result(pr, [finding()]))

    assert not out.posted and "already reviewed" in out.skipped_reason
    assert gh.posted == []


async def test_a_review_of_a_different_commit_does_not_block_this_one(pr):
    gh = FakeGitHub(pr)
    gh.reviews = [{"body": f"{marker_for('older-sha')}\nold review"}]
    out = await ReviewPublisher(gh).publish(pr, make_result(pr, [finding()]))
    assert out.posted


async def test_force_ignores_the_duplicate_check(pr):
    gh = FakeGitHub(pr)
    gh.reviews = [{"body": marker_for("head456")}]
    out = await ReviewPublisher(gh).publish(
        pr, make_result(pr, [finding()]), skip_if_reviewed=False
    )
    assert out.posted


async def test_request_changes_only_escalates_on_critical(pr):
    gh = FakeGitHub(pr)
    pub = ReviewPublisher(gh, event="REQUEST_CHANGES")

    await pub.publish(pr, make_result(pr, [finding(severity="major")]))
    assert gh.posted[-1][1]["event"] == "COMMENT"

    await pub.publish(pr, make_result(pr, [finding(severity="critical")]), skip_if_reviewed=False)
    assert gh.posted[-1][1]["event"] == "REQUEST_CHANGES"


async def test_approve_is_downgraded_when_findings_exist(pr):
    gh = FakeGitHub(pr)
    pub = ReviewPublisher(gh, event="APPROVE")
    await pub.publish(pr, make_result(pr, [finding()]))
    assert gh.posted[-1][1]["event"] == "COMMENT"


async def test_inline_comments_are_capped(pr):
    gh = FakeGitHub(pr)
    many = [finding(title=f"issue {i}", line=1 + (i % 8)) for i in range(80)]
    out = await ReviewPublisher(gh).publish(pr, make_result(pr, many))
    assert out.inline_comments == 50


async def test_static_findings_reach_the_review_body(pr):
    """Regression: the pre-pass told the model "already reported, skip these",
    but the linter findings were never surfaced anywhere - so anything ruff
    caught was silently swallowed instead of merely de-duplicated."""
    from app.review.analysis import StaticFinding

    result = make_result(pr)
    result.known_issues = [
        StaticFinding("ruff", "src/io.py", 8, "S608", "possible SQL injection"),
    ]
    gh = FakeGitHub(pr)
    await ReviewPublisher(gh).publish(pr, result)

    body = gh.posted[0][1]["body"]
    assert "1 issue(s) from static analysis" in body
    assert "possible SQL injection" in body


async def test_body_omits_the_static_section_when_there_is_nothing_to_report(pr):
    gh = FakeGitHub(pr)
    await ReviewPublisher(gh).publish(pr, make_result(pr))
    assert "static analysis" not in gh.posted[0][1]["body"]
