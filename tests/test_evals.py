from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evals.cases import ExpectedBug, load_cases, load_fixture_pr
from app.evals.harness import (
    CaseReport,
    EvalReport,
    gate,
    render_diff,
    render_report,
    score_case,
)
from app.obs.metrics import ReviewMetrics
from app.review.engine import Finding, ReviewResult

EVALS = Path(__file__).resolve().parents[1] / "evals"


def result_with(pr, findings, proposed=None, dropped=0):
    m = ReviewMetrics(
        proposed_findings=proposed if proposed is not None else len(findings) + dropped,
        dropped_findings=dropped,
        kept_findings=len(findings),
    )
    return ReviewResult(pr=pr, summary="s", findings=list(findings), metrics=m)


def finding(file="app/pagination.py", line=13, title="Off-by-one in slice", severity="major"):
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category="correctness",
        title=title,
        detail="returns one extra element",
    )


def test_shipped_cases_load_and_every_expected_line_is_a_real_anchor():
    """A wrong expectation scores a correct review as a miss - guard against it."""
    cases = load_cases(EVALS / "cases.json")
    assert cases
    for case in cases:
        pr, _ = load_fixture_pr(case.fixture, EVALS)
        for bug in case.expected:
            prf = pr.file(bug.file)
            assert prf is not None, f"{case.name}: {bug.file} not in the fixture"
            assert bug.line in prf.patch.commentable, f"{case.name}: line {bug.line}"


def test_fixture_loading_needs_no_network():
    pr, contents = load_fixture_pr("fixtures/sql_injection.json", EVALS)
    assert pr.files[0].path == "store/users.py"
    assert "LIKE" in contents["store/users.py"]
    assert 9 in pr.files[0].patch.added_lines


def test_expected_bug_matching_respects_the_window():
    bug = ExpectedBug(file="a.py", line=10, window=2)
    assert bug.matches("a.py", 10, "anything", "minor")
    assert bug.matches("a.py", 12, "anything", "minor")
    assert not bug.matches("a.py", 13, "anything", "minor")
    assert not bug.matches("b.py", 10, "anything", "minor")


def test_expected_bug_matching_respects_keywords_and_severity():
    bug = ExpectedBug(file="a.py", line=10, keywords=["injection"], min_severity="major")
    assert bug.matches("a.py", 10, "SQL injection risk", "major")
    assert not bug.matches("a.py", 10, "looks fine", "major")
    assert not bug.matches("a.py", 10, "SQL injection risk", "minor")


def test_scoring_counts_hits_misses_and_noise(pr):
    case = load_cases(EVALS / "cases.json")[0]
    fixture_pr, _ = load_fixture_pr(case.fixture, EVALS)
    result = result_with(
        fixture_pr,
        [finding(), finding(line=17, title="unrelated nitpick", severity="minor")],
    )
    report = score_case(case, result)

    assert len(report.found) == 1
    assert len(report.missed) == 1  # the empty-items bug was not reported
    assert len(report.noise) == 1
    assert report.detection == 0.5


def test_a_clean_case_scores_every_finding_as_noise(pr):
    case = next(c for c in load_cases(EVALS / "cases.json") if c.name == "clean-refactor")
    fixture_pr, _ = load_fixture_pr(case.fixture, EVALS)
    report = score_case(case, result_with(fixture_pr, [finding(file="util/strings.py", line=6)]))
    assert report.detection == 1.0  # nothing to find
    assert len(report.noise) == 1  # but it invented something


def test_one_finding_cannot_satisfy_two_expected_bugs():
    from app.evals.cases import EvalCase
    from app.github.client import PullRequest

    case = EvalCase(
        name="x",
        fixture="f",
        expected=[
            ExpectedBug(file="a.py", line=10, description="first"),
            ExpectedBug(file="a.py", line=11, description="second"),
        ],
    )
    pr = PullRequest(owner="o", repo="r", number=1, title="", body="", base_sha="", head_sha="")
    report = score_case(case, result_with(pr, [finding(file="a.py", line=10)]))
    assert len(report.found) == 1 and len(report.missed) == 1


def test_aggregate_metrics_across_cases():
    report = EvalReport(provider="fake", model="m")
    report.cases = [
        CaseReport(
            name="a",
            kind="fixture",
            found=["1"],
            missed=[],
            noise=["n"],
            proposed=4,
            dropped=1,
            tokens=100,
        ),
        CaseReport(
            name="b",
            kind="fixture",
            found=["1"],
            missed=["2"],
            noise=[],
            proposed=6,
            dropped=2,
            tokens=50,
        ),
    ]
    s = report.summary()
    assert s["bugs_found"] == 2 and s["bugs_expected"] == 3
    assert s["detection"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["drop_rate"] == pytest.approx(3 / 10)
    assert s["noise_per_case"] == 0.5
    assert s["total_tokens"] == 150


def test_errored_cases_are_excluded_from_rates_but_counted():
    report = EvalReport()
    report.cases = [
        CaseReport(name="ok", kind="fixture", found=["1"], proposed=1),
        CaseReport(name="bad", kind="fixture", error="boom"),
    ]
    s = report.summary()
    assert s["errors"] == 1 and s["cases"] == 2
    assert s["detection"] == 1.0


def test_report_rendering_is_readable():
    report = EvalReport(provider="fake", model="m")
    report.cases = [
        CaseReport(name="a", kind="fixture", found=["x"], noise=["n"], proposed=2, dropped=1)
    ]
    text = render_report(report, verbose=True)
    assert "detection" in text and "drop rate" in text and "a" in text


def test_baseline_diff_flags_the_ambiguous_win():
    """More bugs found *and* more hallucinated is not obviously an improvement."""
    current = EvalReport()
    current.cases = [CaseReport(name="a", kind="fixture", found=["1", "2"], proposed=10, dropped=4)]
    baseline = {"summary": {"detection": 0.5, "drop_rate": 0.1, "noise_per_case": 1.0}}
    text = render_diff(current, baseline)
    assert "not obviously a win" in text


def test_baseline_diff_reports_a_clean_improvement():
    current = EvalReport()
    current.cases = [CaseReport(name="a", kind="fixture", found=["1", "2"], proposed=10, dropped=1)]
    baseline = {"summary": {"detection": 0.5, "drop_rate": 0.5, "noise_per_case": 2.0}}
    text = render_diff(current, baseline)
    assert "detection better" in text and "drop_rate better" in text
    assert "not obviously a win" not in text


def test_report_serialises_to_json():
    report = EvalReport(provider="p", model="m")
    report.cases = [CaseReport(name="a", kind="fixture")]
    assert json.loads(json.dumps(report.to_dict()))["summary"]["provider"] == "p"


async def test_harness_runs_end_to_end_offline(monkeypatch, settings):
    """The full path: load cases -> review fixtures -> score -> report.

    A perfect reviewer is simulated by returning exactly the planted bugs, so
    this asserts the harness can recognise a correct review - the property that
    makes every other eval number meaningful.
    """
    from app.evals.harness import EvalRunner
    from tests.conftest import FakeProvider

    cases = load_cases(EVALS / "cases.json")
    answers = {
        "off-by-one-slice": [
            {
                "file": "app/pagination.py",
                "line": 13,
                "severity": "major",
                "category": "correctness",
                "title": "Slice returns one extra element",
                "detail": "end + 1 makes pages overlap by one item",
                "confidence": 0.9,
            },
            {
                "file": "app/pagination.py",
                "line": 9,
                "severity": "major",
                "category": "correctness",
                "title": "Empty items clamps page to 0",
                "detail": "total_pages is 0 so start becomes negative",
                "confidence": 0.8,
            },
        ],
        "sql-injection": [
            {
                "file": "store/users.py",
                "line": 9,
                "severity": "critical",
                "category": "security",
                "title": "SQL injection in search_users",
                "detail": "fragment is interpolated into the query",
                "confidence": 0.95,
            },
        ],
        "resource-leak": [
            {
                "file": "io/exporter.py",
                "line": 8,
                "severity": "major",
                "category": "correctness",
                "title": "File handle is never closed",
                "detail": "use a context manager",
                "confidence": 0.9,
            },
            {
                "file": "io/exporter.py",
                "line": 19,
                "severity": "major",
                "category": "error-handling",
                "title": "Exception is swallowed",
                "detail": "except Exception: pass hides every failure",
                "confidence": 0.9,
            },
            {
                "file": "io/exporter.py",
                "line": 18,
                "severity": "major",
                "category": "correctness",
                "title": "Each batch truncates the file",
                "detail": 'reopening in "w" mode overwrites earlier batches',
                "confidence": 0.85,
            },
        ],
        "clean-refactor": [],
    }

    state = {"case": None}

    class Oracle(FakeProvider):
        async def complete_json(self, *, system, user, temperature=0.2):
            for name, findings in answers.items():
                marker = {
                    "off-by-one-slice": "app/pagination.py",
                    "sql-injection": "store/users.py",
                    "resource-leak": "io/exporter.py",
                    "clean-refactor": "util/strings.py",
                }[name]
                if marker in user:
                    state["case"] = name
                    self.payload = {"summary": "s", "findings": findings}
                    break
            return await super().complete_json(system=system, user=user)

    monkeypatch.setattr("app.evals.harness.build_provider", lambda s: Oracle())
    report = await EvalRunner(settings, EVALS / "cases.json").run(cases)

    s = report.summary()
    assert s["errors"] == 0
    assert s["detection"] == 1.0, f"missed: {[c.missed for c in report.cases]}"
    assert s["drop_rate"] == 0.0
    assert s["noise_per_case"] == 0.0
    assert "detection 100%" in render_report(report)


async def test_harness_scores_a_hallucinating_reviewer_badly(monkeypatch, settings):
    from app.evals.harness import EvalRunner
    from tests.conftest import FakeProvider

    cases = [c for c in load_cases(EVALS / "cases.json") if c.name == "sql-injection"]

    class Confabulator(FakeProvider):
        async def complete_json(self, *, system, user, temperature=0.2):
            self.payload = {
                "summary": "s",
                "findings": [
                    {
                        "file": "store/users.py",
                        "line": 4000,
                        "severity": "critical",
                        "category": "security",
                        "title": "ghost",
                        "detail": "d",
                        "confidence": 0.9,
                    },
                    {
                        "file": "does/not/exist.py",
                        "line": 1,
                        "severity": "major",
                        "category": "correctness",
                        "title": "phantom",
                        "detail": "d",
                        "confidence": 0.9,
                    },
                ],
            }
            return await super().complete_json(system=system, user=user)

    monkeypatch.setattr("app.evals.harness.build_provider", lambda s: Confabulator())
    report = await EvalRunner(settings, EVALS / "cases.json").run(cases)

    assert report.detection == 0.0
    assert report.drop_rate == 1.0, "every invented finding must be counted as a drop"


def test_a_bug_caught_only_by_the_linter_counts_as_found():
    """Regression: the pre-pass tells the model not to repeat linter findings,
    so scoring model output alone marked a correctly-suppressed bug as a miss -
    which would send you tuning the prompt to duplicate ruff."""
    from app.review.analysis import StaticFinding

    case = next(c for c in load_cases(EVALS / "cases.json") if c.name == "sql-injection")
    fixture_pr, _ = load_fixture_pr(case.fixture, EVALS)

    result = result_with(fixture_pr, [])  # the model said nothing
    result.known_issues = [
        StaticFinding(
            "ruff",
            "store/users.py",
            9,
            "S608",
            "Possible SQL injection vector through string-based query construction",
        ),
    ]
    report = score_case(case, result)

    assert report.detection == 1.0
    assert report.found_by_linter == 1 and report.found_by_model == 0
    assert "ruff S608" in report.found[0]


def test_model_and_linter_detections_are_attributed_separately():
    from app.review.analysis import StaticFinding

    case = next(c for c in load_cases(EVALS / "cases.json") if c.name == "resource-leak")
    fixture_pr, _ = load_fixture_pr(case.fixture, EVALS)

    result = result_with(
        fixture_pr,
        [
            Finding(
                file="io/exporter.py",
                line=18,
                severity="major",
                category="correctness",
                title="Each batch truncates the file",
                detail='reopening in "w" mode overwrites earlier batches',
            ),
        ],
    )
    result.known_issues = [
        StaticFinding(
            "ruff", "io/exporter.py", 8, "SIM115", "Use a context manager for opening files"
        ),
        StaticFinding(
            "ruff",
            "io/exporter.py",
            19,
            "S110",
            "`try`-`except`-`pass` detected, consider logging the exception",
        ),
    ]
    report = score_case(case, result)

    assert report.found_by_model == 1
    assert report.found_by_linter == 2
    assert report.detection == 1.0
    assert not report.missed


def test_one_linter_finding_cannot_satisfy_two_expected_bugs():
    from app.evals.cases import EvalCase
    from app.github.client import PullRequest
    from app.review.analysis import StaticFinding

    case = EvalCase(
        name="x",
        fixture="f",
        expected=[
            ExpectedBug(file="a.py", line=10, description="first", keywords=["leak"]),
            ExpectedBug(file="a.py", line=11, description="second", keywords=["leak"]),
        ],
    )
    pr = PullRequest(owner="o", repo="r", number=1, title="", body="", base_sha="", head_sha="")
    result = result_with(pr, [])
    result.known_issues = [StaticFinding("ruff", "a.py", 10, "X", "resource leak")]
    report = score_case(case, result)
    assert report.found_by_linter == 1 and len(report.missed) == 1


# --- CI gating ------------------------------------------------------------


def _report(*cases: CaseReport) -> EvalReport:
    report = EvalReport(provider="fake", model="m")
    report.cases = list(cases)
    return report


def test_a_clean_run_passes_the_gate():
    report = _report(CaseReport(name="a", kind="fixture", found=["1"]))
    assert gate(report, min_detection=0.6) == (0, "")


def test_an_errored_case_fails_the_run_by_default():
    """The trap this closes: errored cases are excluded from every metric, so a
    run where the only surviving case did well reports a detection rate CI would
    read as a pass. A partial run is not evidence."""
    report = _report(
        CaseReport(name="ok", kind="fixture", found=["1"]),
        CaseReport(name="quota", kind="fixture", error="LLMError: quota exceeded"),
    )
    assert report.detection == 1.0  # the flattering number, computed from one case

    code, message = gate(report, min_detection=0.6)
    assert code == 1
    assert "quota" in message and "errored" in message


def test_allow_errors_opts_back_into_a_partial_run():
    report = _report(
        CaseReport(name="ok", kind="fixture", found=["1"]),
        CaseReport(name="boom", kind="fixture", error="boom"),
    )
    assert gate(report, min_detection=0.6, allow_errors=True) == (0, "")


def test_errors_are_reported_before_the_metric_gates():
    """An errored run is unusable regardless of what the surviving numbers say,
    so the error is the message, not a detection failure downstream of it."""
    report = _report(CaseReport(name="boom", kind="fixture", error="boom"))
    code, message = gate(report, min_detection=0.9, max_drop_rate=0.0)
    assert code == 1 and "errored" in message and "detection" not in message


def test_detection_below_the_floor_still_fails():
    report = _report(CaseReport(name="a", kind="fixture", found=["1"], missed=["2", "3"]))
    code, message = gate(report, min_detection=0.6)
    assert code == 1 and "detection" in message


def test_drop_rate_above_the_ceiling_still_fails():
    report = _report(CaseReport(name="a", kind="fixture", found=["1"], proposed=10, dropped=5))
    code, message = gate(report, max_drop_rate=0.2)
    assert code == 1 and "drop rate" in message


def test_the_error_warning_says_the_numbers_are_partial():
    report = _report(
        CaseReport(name="ok", kind="fixture", found=["1"]),
        CaseReport(name="boom", kind="fixture", error="boom"),
    )
    assert "excluded from every number" in render_report(report)


def test_a_diff_across_models_says_so():
    """Free-tier quotas make model-hopping routine; a silent diff would lie."""
    from app.evals.harness import EvalReport, render_diff

    current = EvalReport(provider="gemini", model="gemini-3.7-flash")
    baseline = {"summary": {"provider": "gemini", "model": "gemini-3.6-flash", "detection": 0.83}}

    out = render_diff(current, baseline)

    assert "gemini-3.6-flash" in out and "gemini-3.7-flash" in out
    assert "not a prompt change" in out


def test_a_same_model_diff_carries_no_warning():
    from app.evals.harness import EvalReport, render_diff

    current = EvalReport(provider="gemini", model="gemini-3.6-flash")
    baseline = {"summary": {"provider": "gemini", "model": "gemini-3.6-flash", "detection": 0.0}}

    assert "WARNING" not in render_diff(current, baseline)


def test_a_baseline_recorded_with_errors_is_called_out():
    """Quota ran out mid-run and --out wrote the partial result as a baseline.

    Its detection rate covered 3 of 4 cases, which would then have been the
    number every later run was judged against.
    """
    from app.evals.harness import EvalReport, render_diff

    current = EvalReport(provider="gemini", model="m")
    baseline = {"summary": {"provider": "gemini", "model": "m", "detection": 0.83, "errors": 1}}

    out = render_diff(current, baseline)

    assert "errored" in out and "re-record" in out


def test_a_clean_baseline_is_not_called_out():
    from app.evals.harness import EvalReport, render_diff

    current = EvalReport(provider="gemini", model="m")
    baseline = {"summary": {"provider": "gemini", "model": "m", "detection": 0.0, "errors": 0}}

    assert "re-record" not in render_diff(current, baseline)
