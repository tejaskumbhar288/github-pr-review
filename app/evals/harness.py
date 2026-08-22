"""Eval harness (roadmap 6).

The question this answers is the one you cannot answer by reading review output
and nodding: *did that prompt change find more real bugs, or just more bugs?*
So every run reports three numbers together, and they have to move in the right
directions at once:

  - **detection** - of the known bugs, how many were found. Up is good.
  - **noise** - findings that matched no known bug, per case. Some are real
    (the fixtures aren't exhaustive), so this is a pressure gauge, not a defect
    count. Up is suspicious.
  - **drop rate** - findings that failed anchor validation. This is a direct
    hallucination measure. Up is bad, always.

`--baseline` diffs a run against a previous one so a prompt change can be
accepted or rejected on evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..github.client import GitHubClient, parse_pr_url
from ..llm.factory import build_provider
from ..obs.logging import setup_logging
from ..review.analysis import StaticAnalyzer
from ..review.engine import Finding, ReviewEngine, ReviewResult
from .cases import EvalCase, ExpectedBug, load_cases, load_fixture_pr

log = logging.getLogger(__name__)

DEFAULT_CASES = Path("evals/cases.json")


@dataclass
class CaseReport:
    name: str
    kind: str
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    total_findings: int = 0
    found_by_model: int = 0
    found_by_linter: int = 0
    drop_rate: float = 0.0
    dropped: int = 0
    proposed: int = 0
    tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def expected_count(self) -> int:
        return len(self.found) + len(self.missed)

    @property
    def detection(self) -> float:
        return len(self.found) / self.expected_count if self.expected_count else 1.0


@dataclass
class EvalReport:
    cases: list[CaseReport] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @property
    def ok_cases(self) -> list[CaseReport]:
        return [c for c in self.cases if c.error is None]

    def _sum(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.ok_cases)

    @property
    def detection(self) -> float:
        expected = sum(c.expected_count for c in self.ok_cases)
        found = sum(len(c.found) for c in self.ok_cases)
        return found / expected if expected else 0.0

    @property
    def drop_rate(self) -> float:
        proposed = self._sum("proposed")
        return self._sum("dropped") / proposed if proposed else 0.0

    @property
    def noise_per_case(self) -> float:
        cases = self.ok_cases
        return sum(len(c.noise) for c in cases) / len(cases) if cases else 0.0

    @property
    def forbidden_hits(self) -> int:
        return sum(len(c.forbidden_hits) for c in self.ok_cases)

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "cases": len(self.cases),
            "errors": len(self.cases) - len(self.ok_cases),
            "detection": round(self.detection, 4),
            "bugs_found": sum(len(c.found) for c in self.ok_cases),
            "found_by_model": self._sum("found_by_model"),
            "found_by_linter": self._sum("found_by_linter"),
            "bugs_expected": sum(c.expected_count for c in self.ok_cases),
            "noise_per_case": round(self.noise_per_case, 2),
            "drop_rate": round(self.drop_rate, 4),
            "forbidden_hits": self.forbidden_hits,
            "total_tokens": self._sum("tokens"),
            "duration_s": round(self.duration_s, 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "cases": [asdict(c) for c in self.cases]}


def score_case(case: EvalCase, result: ReviewResult) -> CaseReport:
    """Score what the *system* reported, not just what the model said.

    The pre-pass deliberately tells the model not to repeat what the linters
    found, so scoring model findings alone marks a correctly-suppressed bug as a
    miss - and sends you tuning the prompt to re-report things ruff already
    caught. Linter findings count as detections because they now reach the user.
    """
    report = CaseReport(
        name=case.name,
        kind=case.kind,
        total_findings=len(result.findings),
        drop_rate=result.metrics.drop_rate,
        dropped=result.metrics.dropped_findings,
        proposed=result.metrics.proposed_findings,
        tokens=result.metrics.total_tokens,
        latency_ms=result.metrics.total_latency_ms,
    )

    unmatched: list[Finding] = list(result.findings)
    unmatched_static = list(result.known_issues)
    for bug in case.expected:
        hit = _first_match(bug, unmatched)
        if hit is not None:
            unmatched.remove(hit)
            report.found.append(f"{_label(bug)} <- model {hit.file}:{hit.line} {hit.title}")
            report.found_by_model += 1
            continue

        static_hit = _first_static_match(bug, unmatched_static)
        if static_hit is not None:
            unmatched_static.remove(static_hit)
            report.found.append(f"{_label(bug)} <- {static_hit.tool} {static_hit.code}")
            report.found_by_linter += 1
            continue

        report.missed.append(_label(bug))

    for f in unmatched:
        report.noise.append(f"{f.file}:{f.line} [{f.severity}] {f.title}")
        if f.file in case.forbidden_files:
            report.forbidden_hits.append(f"{f.file}:{f.line} {f.title}")

    return report


def _first_match(bug: ExpectedBug, findings: list[Finding]) -> Finding | None:
    for f in findings:
        if bug.matches(f.file, f.line, f"{f.title} {f.detail}", f.severity):
            return f
    return None


def _first_static_match(bug: ExpectedBug, findings: list) -> object | None:
    """Match a linter finding. Severity is not compared - a linter reporting an
    issue at all is the signal; it has no severity scale of its own."""
    for f in findings:
        if bug.matches(f.file, f.line, f"{f.code} {f.message}", bug.min_severity):
            return f
    return None


def _label(bug: ExpectedBug) -> str:
    return f"{bug.file}:{bug.line} {bug.description or '(no description)'}"


class EvalRunner:
    def __init__(self, settings: Settings, cases_path: Path):
        self._settings = settings
        self._cases_dir = cases_path.parent

    async def run(self, cases: list[EvalCase], *, concurrency: int = 1) -> EvalReport:
        started = time.perf_counter()
        llm = build_provider(self._settings)
        gh = GitHubClient(self._settings.github_token, self._settings.github_api)
        engine = ReviewEngine(gh, llm, self._settings, analyzer=StaticAnalyzer(self._settings))

        report = EvalReport(provider=llm.name, model=llm.model)
        sem = asyncio.Semaphore(concurrency)

        async def one(case: EvalCase) -> CaseReport:
            async with sem:
                return await self._run_case(case, engine, gh)

        try:
            results = await asyncio.gather(*(one(c) for c in cases), return_exceptions=True)
        finally:
            await gh.close()
            await llm.close()

        for case, res in zip(cases, results, strict=True):
            if isinstance(res, BaseException):
                log.error("case %s crashed: %s", case.name, res)
                report.cases.append(
                    CaseReport(name=case.name, kind=case.kind, error=f"{type(res).__name__}: {res}")
                )
            else:
                report.cases.append(res)

        report.duration_s = time.perf_counter() - started
        return report

    async def _run_case(self, case: EvalCase, engine: ReviewEngine, gh: GitHubClient) -> CaseReport:
        log.info("running case %s (%s)", case.name, case.kind)
        if case.fixture:
            pr, contents = load_fixture_pr(case.fixture, self._cases_dir)
            # Fixture context is local, so bypass the network fetch entirely.
            result = await engine.review_pr(pr, context=contents)
        else:
            owner, repo, number = parse_pr_url(case.url or "")
            result = await engine.review(owner, repo, number)

        return score_case(case, result)


# --- reporting ------------------------------------------------------------


def render_report(report: EvalReport, *, verbose: bool = False) -> str:
    s = report.summary()
    lines = [
        "",
        f"Eval: {s['cases']} case(s) against {s['provider']}/{s['model']}  "
        f"({s['duration_s']}s, {s['total_tokens']:,} tokens)",
        "",
        f"{'case':<28} {'kind':<8} {'found':>9} {'noise':>6} {'drop':>7}",
        "-" * 62,
    ]
    for c in report.cases:
        if c.error:
            lines.append(f"{c.name:<28} {c.kind:<8} {'ERROR':>9}  {c.error[:24]}")
            continue
        lines.append(
            f"{c.name:<28} {c.kind:<8} "
            f"{len(c.found):>4}/{c.expected_count:<4} "
            f"{len(c.noise):>6} {c.drop_rate:>6.0%}"
        )

    lines += [
        "-" * 62,
        f"detection {s['detection']:.0%} ({s['bugs_found']}/{s['bugs_expected']}"
        f"; {s['found_by_model']} by model, {s['found_by_linter']} by linter)   "
        f"noise/case {s['noise_per_case']}   drop rate {s['drop_rate']:.1%}",
    ]
    if s["forbidden_hits"]:
        lines.append(f"WARNING: {s['forbidden_hits']} finding(s) on forbidden files")
    if s["errors"]:
        lines.append(f"WARNING: {s['errors']} case(s) errored")

    if verbose:
        for c in report.cases:
            if not (c.missed or c.noise):
                continue
            lines.append(f"\n{c.name}:")
            lines.extend(f"  MISSED {m}" for m in c.missed)
            lines.extend(f"  noise  {n}" for n in c.noise)

    return "\n".join(lines)


def render_diff(current: EvalReport, baseline: dict[str, Any]) -> str:
    base = baseline.get("summary", {})
    rows = [
        ("detection", current.detection, base.get("detection", 0.0), "up"),
        ("drop_rate", current.drop_rate, base.get("drop_rate", 0.0), "down"),
        ("noise_per_case", current.noise_per_case, base.get("noise_per_case", 0.0), "down"),
    ]
    lines = ["", "vs baseline:", f"{'metric':<18} {'baseline':>10} {'current':>10} {'delta':>10}"]
    verdict_bits = []
    for name, cur, prev, better in rows:
        delta = cur - prev
        arrow = "→"
        if abs(delta) > 1e-9:
            improved = delta > 0 if better == "up" else delta < 0
            arrow = "✓" if improved else "✗"
            verdict_bits.append(f"{name} {'better' if improved else 'worse'}")
        lines.append(f"{name:<18} {prev:>10.4f} {cur:>10.4f} {delta:>+10.4f}  {arrow}")

    lines.append("")
    lines.append(
        "verdict: " + ("; ".join(verdict_bits) if verdict_bits else "no measurable change")
    )
    if current.detection > base.get("detection", 0.0) and current.drop_rate > base.get(
        "drop_rate", 0.0
    ):
        lines.append(
            "note: detection and drop rate both rose - the prompt is finding more "
            "bugs *and* inventing more. That is not obviously a win."
        )
    return "\n".join(lines)


# --- entrypoint -----------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.provider:
        settings = settings.with_overrides(llm_provider=args.provider)
    if args.model:
        key = "gemini_model" if settings.llm_provider == "gemini" else "ollama_model"
        settings = settings.with_overrides(**{key: args.model})
    settings.validate()

    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c.name in wanted]
    if args.fixtures_only:
        cases = [c for c in cases if c.kind == "fixture"]
    if not cases:
        print("no cases selected", flush=True)
        return 2

    report = await EvalRunner(settings, cases_path).run(cases, concurrency=args.concurrency)
    print(render_report(report, verbose=args.verbose))

    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.is_file():
            print(render_diff(report, json.loads(baseline_path.read_text())))
        else:
            print(f"\n(no baseline at {baseline_path}; this run can become one with --out)")

    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")

    if args.min_detection is not None and report.detection < args.min_detection:
        print(f"\nFAIL: detection {report.detection:.0%} < {args.min_detection:.0%}")
        return 1
    if args.max_drop_rate is not None and report.drop_rate > args.max_drop_rate:
        print(f"\nFAIL: drop rate {report.drop_rate:.1%} > {args.max_drop_rate:.1%}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-review-eval", description="Measure review quality against known-bug PRs"
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--only", nargs="*", help="run only these case names")
    parser.add_argument(
        "--fixtures-only", action="store_true", help="skip live PRs; offline and deterministic"
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--provider", choices=["gemini", "ollama"])
    parser.add_argument("--model")
    parser.add_argument("--out", help="write the full report as JSON")
    parser.add_argument("--baseline", help="compare against a previous --out file")
    parser.add_argument("--min-detection", type=float, help="exit 1 below this detection rate")
    parser.add_argument("--max-drop-rate", type=float, help="exit 1 above this drop rate")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging("INFO" if args.verbose else "WARNING")
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
