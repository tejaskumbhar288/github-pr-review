"""The review pipeline: fetch -> contextualize -> pre-analyse -> reason -> validate."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..github.client import GitHubClient, PullRequest
from ..llm.base import LLMProvider
from ..obs.metrics import ReviewMetrics
from ..obs.tracing import Tracer
from .analysis import StaticAnalyzer, StaticFinding
from .prompt import SYSTEM, build_user_prompt
from .repo_context import RepoContext, RepoContextBuilder

log = logging.getLogger(__name__)

SEVERITIES = ("critical", "major", "minor")
CATEGORIES = frozenset(
    {"correctness", "security", "performance", "error-handling", "maintainability"}
)
SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

# Findings below this confidence are dropped. The prompt asks the model to omit
# them itself; this is the backstop for when it doesn't.
MIN_CONFIDENCE = 0.4
MAX_FINDINGS = 25
MAX_DETAIL_CHARS = 2000
MAX_SUGGESTION_CHARS = 2000

# Models sometimes fall into a degeneration loop. Two shapes show up in
# practice, and a character cap catches neither - it just truncates the mess:
#   1. the same token repeated ("context context context ...")
#   2. low-diversity word salad drawn from a small vocabulary
#      ("structure pattern logic setup parameters structure pattern logic ...")
# Both are worse than having no suggestion at all.
DEGENERATE_RUN = 6
# A run of bare words this long with this little vocabulary is not prose.
SALAD_MIN_TOKENS = 15
SALAD_MAX_DIVERSITY = 0.6


@dataclass
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    detail: str
    suggestion: str | None = None
    confidence: float = 1.0
    snapped_from: int | None = None
    """Set when the model's line was off by a little and we snapped it."""

    @property
    def dedupe_key(self) -> tuple[str, int, str]:
        return (self.file, self.line, self.title.strip().lower())


@dataclass
class ReviewResult:
    pr: PullRequest
    summary: str
    findings: list[Finding] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    """Findings rejected by validation, kept for observability."""
    known_issues: list[StaticFinding] = field(default_factory=list)
    repo_context: RepoContext = field(default_factory=RepoContext)
    """Callers pulled in from elsewhere in the repo; see review.repo_context."""
    metrics: ReviewMetrics = field(default_factory=ReviewMetrics)

    @property
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(SEVERITIES, 0)
        for f in self.findings:
            out[f.severity] += 1
        return out

    @property
    def has_blocking(self) -> bool:
        return self.counts["critical"] > 0


class ReviewEngine:
    def __init__(
        self,
        gh: GitHubClient,
        llm: LLMProvider,
        settings: Settings,
        tracer: Tracer | None = None,
        analyzer: StaticAnalyzer | None = None,
        repo_context: RepoContextBuilder | None = None,
    ):
        self._gh = gh
        self._llm = llm
        self._settings = settings
        self._tracer = tracer or Tracer()
        self._analyzer = analyzer or StaticAnalyzer(settings)
        self._repo_context = repo_context or RepoContextBuilder(gh, settings)

    async def review(self, owner: str, repo: str, number: int) -> ReviewResult:
        started = time.perf_counter()
        pr = await self._gh.fetch_pr(
            owner,
            repo,
            number,
            max_files=self._settings.max_files,
            max_patch_lines=self._settings.max_patch_lines,
        )
        return await self.review_pr(pr, started=started)

    async def review_pr(
        self,
        pr: PullRequest,
        *,
        started: float | None = None,
        context: dict[str, str] | None = None,
    ) -> ReviewResult:
        """Review a PR. ``context`` overrides the whole-file fetch, which lets
        offline callers (the eval harness) supply contents without patching the
        engine - a shared engine cannot be monkeypatched safely under concurrency.
        """
        started = started if started is not None else time.perf_counter()
        metrics = ReviewMetrics(
            pr=pr.slug,
            head_sha=pr.head_sha,
            provider=self._llm.name,
            model=self._llm.model,
            files_reviewed=len(pr.files),
            files_excluded=pr.truncated_files,
        )

        reviewable = [f for f in pr.files if not f.patch.is_empty]
        if not reviewable:
            metrics.total_latency_ms = (time.perf_counter() - started) * 1000
            self._tracer.record(metrics)
            return ReviewResult(
                pr=pr,
                summary="No reviewable source changes in this PR.",
                metrics=metrics,
            )

        with self._tracer.review(
            "pr-review", pr=pr.slug, head_sha=pr.head_sha, files=len(pr.files)
        ) as span:
            try:
                # An offline caller supplying context is also saying "do not go
                # to the network", so the caller expansion is skipped with it -
                # not silently, but because there is no repo to search.
                offline = context is not None
                context = context if context is not None else await self._gather_context(pr)
                metrics.context_files = len(context)
                metrics.context_chars = sum(len(c) for c in context.values())

                known_issues = await self._analyzer.analyze(pr, context)
                metrics.static_findings = len(known_issues)
                metrics.static_suppressed = self._analyzer.suppressed_count

                repo_context = (
                    RepoContext(reason="offline")
                    if offline
                    else await self._repo_context.build(pr, context)
                )
                metrics.repo_context_files = repo_context.files
                metrics.repo_context_searches = repo_context.searches
                if repo_context.reason:
                    log.debug("no caller context: %s", repo_context.reason)

                user_prompt = build_user_prompt(pr, context, known_issues, repo_context)
                metrics.prompt_chars = len(user_prompt) + len(SYSTEM)

                generation = span.generation(
                    name="review-completion",
                    model=self._llm.model,
                    input={"system_chars": len(SYSTEM), "user_chars": len(user_prompt)},
                )
                response = await self._llm.complete_json(system=SYSTEM, user=user_prompt)

                metrics.prompt_tokens = response.usage.prompt_tokens
                metrics.completion_tokens = response.usage.completion_tokens
                metrics.llm_latency_ms = response.latency_ms
                metrics.attempts = response.attempts
                generation.update(
                    usage_details=response.usage.as_dict(),
                    output={"findings": len(response.data.get("findings") or [])},
                )
                generation.end()

                result = self._validate(pr, response.data, metrics)
                result.known_issues = known_issues
                result.repo_context = repo_context
            except Exception as exc:
                metrics.error = f"{type(exc).__name__}: {exc}"
                metrics.total_latency_ms = (time.perf_counter() - started) * 1000
                self._tracer.record(metrics)
                raise

            metrics.total_latency_ms = (time.perf_counter() - started) * 1000
            span.update(
                output={
                    "kept": metrics.kept_findings,
                    "dropped": metrics.dropped_findings,
                    "drop_rate": metrics.drop_rate,
                }
            )

        self._tracer.record(metrics)
        return result

    # --- context ----------------------------------------------------------

    async def _gather_context(self, pr: PullRequest) -> dict[str, str]:
        """Pull whole-file contents so findings can reason about surroundings.

        This is the single biggest quality lever, and it is why a large context
        window matters more here than raw model speed. Fetches run concurrently
        under a semaphore - forty sequential round trips used to dominate the
        wall-clock time of a review.
        """
        wanted = [f for f in pr.files if not f.is_deleted and not f.patch.is_empty]
        if not wanted or self._settings.context_char_limit <= 0:
            return {}

        sem = asyncio.Semaphore(self._settings.context_concurrency)

        async def fetch(path: str) -> tuple[str, str | None]:
            async with sem:
                return path, await self._gh.fetch_file(pr.owner, pr.repo, path, pr.head_sha)

        results = await asyncio.gather(*(fetch(f.path) for f in wanted), return_exceptions=True)

        context: dict[str, str] = {}
        for item in results:
            if isinstance(item, BaseException):
                log.debug("context fetch failed: %s", item)
                continue
            path, content = item
            if not content:
                continue
            if len(content) > self._settings.context_char_limit:
                log.debug("skipping oversized context for %s (%d chars)", path, len(content))
                continue
            context[path] = content
        return context

    # --- validation -------------------------------------------------------

    def _validate(
        self, pr: PullRequest, raw: dict[str, Any], metrics: ReviewMetrics
    ) -> ReviewResult:
        """Reject anything the model hallucinated before it reaches GitHub."""
        by_path = {f.path: f for f in pr.files}
        result = ReviewResult(
            pr=pr,
            summary=str(raw.get("summary") or "").strip() or "(no summary returned)",
            metrics=metrics,
        )

        raw_findings = raw.get("findings")
        if not isinstance(raw_findings, list):
            raw_findings = []
        metrics.proposed_findings = len(raw_findings)

        reasons: Counter[str] = Counter()
        seen: set[tuple[str, int, str]] = set()

        def drop(reason: str, detail: str) -> None:
            reasons[reason] += 1
            result.dropped.append(detail)

        for item in raw_findings:
            if not isinstance(item, dict):
                drop("malformed", "non-object finding")
                continue

            path = str(item.get("file") or "").strip().lstrip("./")
            pr_file = by_path.get(path)
            if pr_file is None:
                drop("unknown_file", f"unknown file {path!r}")
                continue

            try:
                line = int(item.get("line"))
            except (TypeError, ValueError):
                drop("bad_line", f"{path}: non-numeric line {item.get('line')!r}")
                continue

            anchor = pr_file.patch.nearest_anchor(line)
            if anchor is None:
                drop("unanchored", f"{path}:{line} is not part of the diff")
                continue
            snapped_from = line if anchor != line else None
            if snapped_from is not None:
                metrics.snapped_findings += 1

            title = str(item.get("title") or "").strip()
            if not title:
                drop("empty_title", f"{path}:{anchor} empty title")
                continue

            confidence = _as_float(item.get("confidence"), default=1.0)
            if confidence < MIN_CONFIDENCE:
                drop("low_confidence", f"{path}:{anchor} confidence {confidence:.2f} — {title}")
                continue

            severity = str(item.get("severity") or "minor").strip().lower()
            category = str(item.get("category") or "maintainability").strip().lower()
            suggestion = item.get("suggestion")

            finding = Finding(
                file=path,
                line=anchor,
                severity=severity if severity in SEVERITY_ORDER else "minor",
                category=category if category in CATEGORIES else "maintainability",
                title=title[:200],
                detail=str(item.get("detail") or "").strip()[:MAX_DETAIL_CHARS],
                suggestion=_clean_suggestion(suggestion),
                confidence=confidence,
                snapped_from=snapped_from,
            )

            if finding.dedupe_key in seen:
                metrics.duplicate_findings += 1
                drop("duplicate", f"{path}:{anchor} duplicate — {title}")
                continue
            seen.add(finding.dedupe_key)
            result.findings.append(finding)

        result.findings.sort(
            key=lambda f: (SEVERITY_ORDER[f.severity], -f.confidence, f.file, f.line)
        )
        if len(result.findings) > MAX_FINDINGS:
            for extra in result.findings[MAX_FINDINGS:]:
                drop("over_limit", f"{extra.file}:{extra.line} beyond the {MAX_FINDINGS} cap")
            result.findings = result.findings[:MAX_FINDINGS]

        counts = result.counts
        metrics.kept_findings = len(result.findings)
        metrics.dropped_findings = len(result.dropped)
        metrics.drop_reasons = dict(reasons)
        metrics.critical = counts["critical"]
        metrics.major = counts["major"]
        metrics.minor = counts["minor"]
        return result


def _clean_suggestion(raw: Any) -> str | None:
    """Normalise a suggestion, discarding degenerate model output."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return None
    text = _strip_repetition(text)[:MAX_SUGGESTION_CHARS].rstrip()
    return text or None


def _strip_repetition(text: str) -> str:
    """Cut the text where a degeneration loop begins.

    Everything before the loop is usually a perfectly good suggestion, so both
    checks truncate rather than discard.
    """
    return _strip_word_salad(_strip_exact_repetition(text))


def _strip_exact_repetition(text: str) -> str:
    """Truncate at a token repeated DEGENERATE_RUN times consecutively."""
    tokens = text.split(" ")
    run_start = 0
    run_token = None
    for i, token in enumerate(tokens):
        stripped = token.strip()
        if stripped and stripped == run_token:
            if i - run_start + 1 >= DEGENERATE_RUN:
                return " ".join(tokens[:run_start]).rstrip()
            continue
        run_token = stripped
        run_start = i
    return text


def _strip_word_salad(text: str) -> str:
    """Truncate at a long run of bare words drawn from a tiny vocabulary.

    Real code carries punctuation, newlines and symbols; real prose keeps
    introducing new words. A long stretch of plain alphabetic tokens that keeps
    recycling the same two dozen words is neither.
    """
    tokens = text.split()
    if len(tokens) < SALAD_MIN_TOKENS:
        return text

    # Map each token back to where it starts, so truncation keeps original spacing.
    offsets: list[int] = []
    cursor = 0
    for token in tokens:
        cursor = text.index(token, cursor)
        offsets.append(cursor)
        cursor += len(token)

    run_start = 0
    for i, token in enumerate(tokens + [""]):
        if token.isalpha():
            continue
        run = tokens[run_start:i]
        if len(run) >= SALAD_MIN_TOKENS:
            lowered = [t.lower() for t in run]
            if len(set(lowered)) / len(lowered) < SALAD_MAX_DIVERSITY:
                return text[: offsets[run_start]].rstrip()
        run_start = i + 1
    return text


def _as_float(value: Any, *, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return min(max(num, 0.0), 1.0)
