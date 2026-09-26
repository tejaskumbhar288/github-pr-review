"""The test-path suppression list (progress note 5).

The bug this covers is a quality one rather than a crash: of 14 static findings
on the first real review this tool posted, ten were `S101 Use of assert
detected` on test files. These tests pin both halves of the fix - that the
irrelevant rules go, and that nothing else does.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.github.client import PRFile, PullRequest
from app.github.patch import parse_patch
from app.review.analysis import (
    StaticAnalyzer,
    StaticFinding,
    is_known_irrelevant,
    is_test_path,
)

TEST_PATCH = """@@ -0,0 +1,4 @@
+def test_adds(client):
+    assert client.add(1, 2) == 3
+    token = "hunter2"
+    assert token
"""


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_engine.py",
        "tests/conftest.py",
        "app/tests/helpers.py",
        "src/__tests__/widget.js",
        "web/widget.test.ts",
        "web/widget.spec.tsx",
        "pkg/handler_test.go",
        "spec/models/user_spec.rb",
        "e2e/checkout.py",
    ],
)
def test_recognises_test_paths(path):
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "app/testing_utils.py",
        "src/protest.py",
        "app/contest/models.py",
        "app/review/engine.py",
        "src/latest.py",
        "",
    ],
)
def test_does_not_mistake_production_code_for_tests(path):
    """The expensive direction. A false positive here silences a real finding."""
    assert not is_test_path(path)


def test_suppresses_only_the_listed_rules_and_only_in_tests():
    assert is_known_irrelevant(StaticFinding("ruff", "tests/test_x.py", 2, "S101", "assert"))
    # A real bug in a test file is still a real bug.
    assert not is_known_irrelevant(
        StaticFinding("ruff", "tests/test_x.py", 2, "F821", "undefined name")
    )
    # And the same rule on production code is untouched.
    assert not is_known_irrelevant(StaticFinding("ruff", "app/core.py", 2, "S101", "assert"))


def _pr_with(path: str) -> PullRequest:
    return PullRequest(
        owner="acme",
        repo="widget",
        number=1,
        title="t",
        body="",
        base_sha="b",
        head_sha="h",
        files=[
            PRFile(
                path=path, status="added", additions=4, deletions=0, patch=parse_patch(TEST_PATCH)
            )
        ],
    )


async def _known_issues(path: str, **over):
    settings = Settings.from_env({"STATIC_ANALYSIS": "true", **over})
    analyzer = StaticAnalyzer(settings)
    if "ruff" not in analyzer.available_tools():
        pytest.skip("ruff is not installed")
    pr = _pr_with(path)
    # The file body exactly as the patch adds it, so every line is commentable.
    contents = {path: "\n".join(ln[1:] for ln in TEST_PATCH.splitlines()[1:])}
    return analyzer, await analyzer.analyze(pr, contents)


async def test_ruff_noise_on_a_test_file_is_dropped_end_to_end():
    analyzer, findings = await _known_issues("tests/test_thing.py")
    codes = {f.code for f in findings}
    assert "S101" not in codes, f"S101 survived: {[f.render() for f in findings]}"
    assert analyzer.suppressed_count > 0


async def test_the_same_file_outside_tests_keeps_its_findings():
    _, findings = await _known_issues("app/thing.py")
    assert "S101" in {f.code for f in findings}


async def test_the_suppression_can_be_turned_off():
    analyzer, findings = await _known_issues("tests/test_thing.py", SUPPRESS_TEST_NOISE="0")
    assert "S101" in {f.code for f in findings}
    assert analyzer.suppressed_count == 0
