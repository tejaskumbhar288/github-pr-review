from __future__ import annotations

import json

from app.cli import render, to_json
from app.github.publisher import PublishResult
from app.obs.metrics import ReviewMetrics
from app.pipeline import ReviewOutcome
from app.review.engine import Finding, ReviewResult


def outcome(pr, findings=(), dropped=(), published=None, metrics=None):
    result = ReviewResult(
        pr=pr,
        summary="Adds a save() helper.",
        findings=list(findings),
        dropped=list(dropped),
        metrics=metrics or ReviewMetrics(model="fake-1"),
    )
    return ReviewOutcome(result=result, published=published)


def finding(**over):
    base = {
        "file": "src/io.py",
        "line": 8,
        "severity": "critical",
        "category": "security",
        "title": "Unclosed handle",
        "detail": "leaks a descriptor",
        "suggestion": "use with",
    }
    base.update(over)
    return Finding(**base)


def test_clean_review_renders_the_good_outcome(pr):
    text = render(outcome(pr), color=False)
    assert "No blocking issues found." in text
    assert "acme/widget#7" in text


def test_findings_render_with_anchors_and_severity(pr):
    text = render(outcome(pr, [finding()]), color=False)
    assert "[CRITICAL] src/io.py:8  (security)" in text
    assert "Unclosed handle" in text
    assert "suggested:" in text


def test_snapped_anchors_are_disclosed(pr):
    text = render(outcome(pr, [finding(snapped_from=11)]), color=False)
    assert "snapped from 11" in text


def test_dropped_findings_are_surfaced_with_the_drop_rate(pr):
    m = ReviewMetrics(proposed_findings=4, dropped_findings=2, kept_findings=2)
    text = render(outcome(pr, [finding()], ["src/io.py:99 not in diff"], metrics=m), color=False)
    assert "Dropped 1 unanchored finding(s)" in text
    assert "drop rate 50%" in text
    assert "src/io.py:99 not in diff" in text


def test_publish_result_is_reported(pr):
    pub = PublishResult(posted=True, inline_comments=2, url="https://x/y#review-1")
    text = render(outcome(pr, [finding()], published=pub), color=False)
    assert "Posted review with 2 inline comment(s)" in text
    assert "https://x/y#review-1" in text


def test_degraded_publish_is_disclosed_not_hidden(pr):
    pub = PublishResult(posted=True, inline_comments=0, degraded=True, url="u")
    text = render(outcome(pr, [finding()], published=pub), color=False)
    assert "summary only" in text


def test_skipped_publish_is_explained(pr):
    pub = PublishResult(posted=False, skipped_reason="already reviewed at this commit")
    text = render(outcome(pr, published=pub), color=False)
    assert "Not posted: already reviewed at this commit" in text


def test_color_can_be_disabled(pr):
    assert "\033[" not in render(outcome(pr, [finding()]), color=False)
    assert "\033[" in render(outcome(pr, [finding()]), color=True)


def test_json_output_is_machine_readable(pr):
    m = ReviewMetrics(proposed_findings=2, dropped_findings=1, kept_findings=1)
    data = json.loads(to_json(outcome(pr, [finding()], ["a"], metrics=m)))
    assert data["pr"] == "acme/widget#7"
    assert data["head_sha"] == "head456"
    assert data["findings"][0]["line"] == 8
    assert data["dropped"] == ["a"]
    assert data["metrics"]["drop_rate"] == 0.5


def test_metrics_block_is_opt_in(pr):
    assert "drop_rate" not in render(outcome(pr), color=False)
    assert "drop_rate" in render(outcome(pr), color=False, show_metrics=True)


def test_excluded_file_count_is_shown(pr):
    pr.truncated_files = 3
    assert "3 excluded" in render(outcome(pr), color=False)


def test_static_findings_are_shown_to_the_user(pr):
    """They are excluded from the model's scope, so the CLI must report them."""
    from app.review.analysis import StaticFinding

    out = outcome(pr)
    out.result.known_issues = [
        StaticFinding("ruff", "src/io.py", 8, "S608", "possible SQL injection"),
    ]
    text = render(out, color=False)
    assert "1 issue(s) from static analysis" in text
    assert "possible SQL injection" in text
