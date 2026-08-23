"""The live eval cases, checked against the real pull requests they name.

A live case is only as good as its line numbers, and unlike a fixture there is
nothing local to validate them against - the PR is the source of truth. GitHub
also only accepts a comment on a line that is part of the diff, so an
expectation anchored anywhere else can never be matched no matter how good the
review is, and would quietly show up as a permanent miss.

Skipped when GITHUB_TOKEN is unset, so the default suite stays offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings
from app.evals.cases import load_cases
from app.github.client import GitHubClient, parse_pr_url

pytestmark = pytest.mark.skipif(not os.getenv("GITHUB_TOKEN"), reason="GITHUB_TOKEN is not set")

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases.live.json"


@pytest.fixture
def live_settings() -> Settings:
    return Settings.from_env({**os.environ, "LLM_PROVIDER": "ollama"})


async def test_every_live_case_still_has_a_diff(live_settings):
    """GitHub stops serving a PR's diff once the contributor deletes their fork."""
    gh = GitHubClient(live_settings.github_token, live_settings.github_api)
    try:
        for case in load_cases(CASES):
            owner, repo, number = parse_pr_url(case.url)
            pr = await gh.fetch_pr(
                owner,
                repo,
                number,
                max_files=live_settings.max_files,
                max_patch_lines=live_settings.max_patch_lines,
            )
            assert pr.files, f"{case.name}: {case.url} no longer returns any files"
    finally:
        await gh.close()


async def test_every_expected_line_is_commentable_on_the_real_pr(live_settings):
    gh = GitHubClient(live_settings.github_token, live_settings.github_api)
    try:
        for case in load_cases(CASES):
            owner, repo, number = parse_pr_url(case.url)
            pr = await gh.fetch_pr(
                owner,
                repo,
                number,
                max_files=live_settings.max_files,
                max_patch_lines=live_settings.max_patch_lines,
            )
            for bug in case.expected:
                prf = pr.file(bug.file)
                assert prf is not None, f"{case.name}: {bug.file} is not a reviewable file"
                assert bug.line in prf.patch.commentable, (
                    f"{case.name}: {bug.file}:{bug.line} is not commentable on the PR"
                )
    finally:
        await gh.close()
