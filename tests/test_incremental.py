"""Incremental review (roadmap 1).

The dangerous failure here is silent: a review that covers two commits while
reading as a review of the whole PR. So these tests care as much about *when the
narrowing is refused* as about when it happens.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.config import Settings
from app.github.client import Comparison, GitHubError, PRFile
from app.github.patch import parse_patch
from app.github.publisher import ReviewPublisher, build_body, marker_for
from app.obs.metrics import ReviewMetrics
from app.review.engine import ReviewResult
from app.review.incremental import IncrementalOutcome, narrow_to_new_commits
from tests.conftest import SAMPLE_PATCH, FakeGitHub

NEW_PATCH = """@@ -20,3 +20,5 @@ def save(path, data):
 def save(path, data):
-    open(path, "w").write(data)
+    with open(path, "w") as fh:
+        fh.write(data)
"""


def comparison(status="ahead", ahead_by=2, files=None) -> Comparison:
    return Comparison(
        status=status,
        ahead_by=ahead_by,
        commits=ahead_by,
        files=files
        if files is not None
        else [
            PRFile(
                path="src/io.py",
                status="modified",
                additions=2,
                deletions=1,
                patch=parse_patch(NEW_PATCH),
            )
        ],
    )


class ComparingGitHub(FakeGitHub):
    def __init__(self, pr, result: Any):
        super().__init__(pr)
        self.result = result
        self.compares: list[tuple[str, str]] = []

    async def compare(self, owner, repo, base, head, *, max_files, max_patch_lines):
        self.compares.append((base, head))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env({"LLM_PROVIDER": "ollama", "STATIC_ANALYSIS": "false"})


async def test_narrows_to_the_new_commits(pr, settings):
    gh = ComparingGitHub(pr, comparison())
    narrowed, mode = await narrow_to_new_commits(gh, pr, "old-sha", settings)

    assert mode == IncrementalOutcome.NARROWED
    assert gh.compares == [("old-sha", "head456")]
    assert narrowed.is_incremental and narrowed.incremental_base == "old-sha"
    assert narrowed.new_commits == 2
    # The diff is the increment's, not the PR's.
    assert narrowed.files[0].patch.annotated != pr.files[0].patch.annotated


@pytest.mark.parametrize("status", ["diverged", "behind"])
async def test_a_rewritten_history_falls_back_to_the_whole_pr(pr, settings, status):
    """After a force-push the diff between the two commits mixes new work with
    rewritten old work, and reviewing it as an increment would report the
    author's rebase as changes."""
    gh = ComparingGitHub(pr, comparison(status=status))
    out, mode = await narrow_to_new_commits(gh, pr, "old-sha", settings)

    assert mode == IncrementalOutcome.DIVERGED
    assert out is pr and not out.is_incremental


async def test_a_failed_compare_falls_back_rather_than_failing(pr, settings):
    """The commit we reviewed may simply be gone. That is not an error worth
    losing a review over."""
    gh = ComparingGitHub(pr, GitHubError("not found: /compare"))
    out, mode = await narrow_to_new_commits(gh, pr, "gone-sha", settings)

    assert mode == IncrementalOutcome.UNAVAILABLE
    assert out is pr and not out.is_incremental


async def test_new_commits_that_touch_nothing_reviewable(pr, settings):
    gh = ComparingGitHub(pr, comparison(files=[]))
    out, mode = await narrow_to_new_commits(gh, pr, "old-sha", settings)

    assert mode == IncrementalOutcome.NOTHING_NEW
    assert out.files == [] and out.new_commits == 2


async def test_the_same_sha_is_not_an_increment(pr, settings):
    gh = ComparingGitHub(pr, comparison())
    out, mode = await narrow_to_new_commits(gh, pr, pr.head_sha, settings)

    assert mode == IncrementalOutcome.FULL
    assert gh.compares == [], "comparing a commit against itself is a wasted request"


# --- what the reader sees --------------------------------------------------


def test_the_posted_body_says_the_review_was_partial(pr):
    """A reader who does not know the scope reads 'no blocking issues found' as
    a verdict on the whole PR."""
    incremental = replace(pr, incremental_base="abc1234def", new_commits=3)
    body = build_body(
        incremental,
        ReviewResult(pr=incremental, summary="Fine.", metrics=ReviewMetrics(model="m")),
    )
    assert "Incremental review" in body
    assert "3 commit(s)" in body and "abc1234" in body
    # And above the summary, not buried in the footer.
    assert body.index("Incremental review") < body.index("Fine.")


def test_a_full_review_says_nothing_about_scope(pr):
    body = build_body(pr, ReviewResult(pr=pr, summary="Fine.", metrics=ReviewMetrics()))
    assert "Incremental" not in body


# --- the history lookup ----------------------------------------------------


async def test_last_reviewed_sha_ignores_reviews_of_the_current_head(pr):
    gh = FakeGitHub(pr)
    gh.reviews = [
        {"id": 1, "body": f"{marker_for('sha-one')} first"},
        {"id": 2, "body": f"{marker_for('sha-two')} second"},
        {"id": 3, "body": f"{marker_for(pr.head_sha)} current"},
    ]
    assert await ReviewPublisher(gh).last_reviewed_sha(pr) == "sha-two"


async def test_review_history_ignores_other_peoples_reviews(pr):
    gh = FakeGitHub(pr)
    gh.reviews = [
        {"id": 1, "body": "LGTM, ship it"},
        {"id": 2, "body": f"{marker_for('sha-two')} ours"},
    ]
    history = await ReviewPublisher(gh).review_history(pr)
    assert [r.sha for r in history] == ["sha-two"]


async def test_no_prior_review_means_no_starting_point(pr):
    assert await ReviewPublisher(FakeGitHub(pr)).last_reviewed_sha(pr) is None


def test_sample_patch_is_still_what_these_tests_assume():
    assert "def save" in SAMPLE_PATCH
