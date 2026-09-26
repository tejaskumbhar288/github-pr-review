"""CLI: review a pull request from the terminal.

python -m app.cli https://github.com/OWNER/REPO/pull/123
python -m app.cli <url> --post          # publish the review to GitHub
python -m app.cli <url> --json          # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from .config import ConfigError, Settings, get_settings
from .github.client import GitHubError, parse_pr_url
from .llm.base import LLMError
from .obs.logging import setup_logging
from .pipeline import ReviewOutcome, ReviewSession
from .review.engine import ReviewResult

COLORS = {"critical": "\033[91m", "major": "\033[93m", "minor": "\033[96m"}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[92m"


def render(outcome: ReviewOutcome, *, color: bool = True, show_metrics: bool = False) -> str:
    result = outcome.result

    def c(code: str) -> str:
        return code if color else ""

    scope = ""
    if result.pr.is_incremental:
        scope = (
            f" — incremental: {result.pr.new_commits} commit(s) since "
            f"{result.pr.incremental_base[:7]}"
        )

    lines = [
        f"\n{c(BOLD)}{result.pr.slug} — {result.pr.title}{c(RESET)}{c(DIM)}{scope}{c(RESET)}",
        f"{c(DIM)}{len(result.pr.files)} file(s) reviewed"
        + (f", {result.pr.truncated_files} excluded" if result.pr.truncated_files else "")
        + (
            f", {len(result.known_issues)} known issue(s) from static analysis"
            if result.known_issues
            else ""
        )
        + f"{c(RESET)}\n",
        result.summary,
        "",
    ]

    if not result.findings:
        lines.append(f"{c(GREEN)}No blocking issues found.{c(RESET)}")
    else:
        counts = result.counts
        lines.append(
            f"{c(BOLD)}{counts['critical']} critical · "
            f"{counts['major']} major · {counts['minor']} minor{c(RESET)}\n"
        )
        for f in result.findings:
            col = c(COLORS.get(f.severity, ""))
            anchor = f"{f.file}:{f.line}"
            if f.snapped_from is not None:
                anchor += f" {c(DIM)}(snapped from {f.snapped_from}){c(RESET)}"
            lines.append(f"{col}[{f.severity.upper()}]{c(RESET)} {anchor}  ({f.category})")
            lines.append(f"  {c(BOLD)}{f.title}{c(RESET)}")
            if f.detail:
                lines.append(f"  {f.detail}")
            if f.suggestion:
                indented = "\n".join(f"    {ln}" for ln in f.suggestion.splitlines())
                lines.append(f"  {c(DIM)}suggested:{c(RESET)}\n{indented}")
            lines.append("")

    if result.repo_context.call_sites:
        lines.append(
            f"{c(DIM)}{len(result.repo_context.call_sites)} call site(s) in "
            f"{result.repo_context.files} file(s) pulled in for "
            f"{', '.join(result.repo_context.symbols)}{c(RESET)}\n"
        )

    if result.known_issues:
        lines.append(
            f"{c(DIM)}{len(result.known_issues)} issue(s) from static analysis "
            f"(excluded from the model's scope):{c(RESET)}"
        )
        lines.extend(f"{c(DIM)}  - {k.render()}{c(RESET)}" for k in result.known_issues[:15])
        if len(result.known_issues) > 15:
            lines.append(f"{c(DIM)}  ... and {len(result.known_issues) - 15} more{c(RESET)}")
        lines.append("")

    if result.dropped:
        lines.append(
            f"{c(DIM)}Dropped {len(result.dropped)} unanchored finding(s) "
            f"— drop rate {result.metrics.drop_rate:.0%}:{c(RESET)}"
        )
        lines.extend(f"{c(DIM)}  - {d}{c(RESET)}" for d in result.dropped)
        lines.append("")

    if outcome.published:
        pub = outcome.published
        if pub.posted:
            note = " (summary only — inline anchoring rejected)" if pub.degraded else ""
            lines.append(
                f"{c(GREEN)}Posted review with {pub.inline_comments} inline "
                f"comment(s){note}:{c(RESET)} {pub.url or ''}"
            )
        else:
            lines.append(f"{c(DIM)}Not posted: {pub.skipped_reason}{c(RESET)}")

    if show_metrics:
        lines.append(f"\n{c(DIM)}{json.dumps(result.metrics.as_dict(), indent=2)}{c(RESET)}")

    return "\n".join(lines)


def to_json(outcome: ReviewOutcome) -> str:
    result: ReviewResult = outcome.result
    return json.dumps(
        {
            "pr": result.pr.slug,
            "head_sha": result.pr.head_sha,
            "scope": outcome.incremental,
            "incremental_base": result.pr.incremental_base,
            "summary": result.summary,
            "findings": [asdict(f) for f in result.findings],
            "dropped": result.dropped,
            "known_issues": [asdict(k) for k in result.known_issues],
            "repo_context": {
                "symbols": result.repo_context.symbols,
                "files": result.repo_context.files,
                "call_sites": len(result.repo_context.call_sites),
                "reason": result.repo_context.reason,
            },
            "metrics": result.metrics.as_dict(),
            "published": asdict(outcome.published) if outcome.published else None,
        },
        indent=2,
    )


def build_settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    overrides: dict[str, object] = {}
    if args.provider:
        overrides["llm_provider"] = args.provider
    if args.model:
        key = (
            "gemini_model"
            if (args.provider or settings.llm_provider) == "gemini"
            else "ollama_model"
        )
        overrides[key] = args.model
    if args.max_files:
        overrides["max_files"] = args.max_files
    if args.no_static:
        overrides["static_analysis"] = False
    if args.no_context:
        overrides["context_char_limit"] = 0
    if args.repo_context:
        overrides["repo_context"] = True
    if args.request_changes:
        overrides["review_event"] = "REQUEST_CHANGES"
    return settings.with_overrides(**overrides) if overrides else settings


async def run(args: argparse.Namespace) -> int:
    owner, repo, number = parse_pr_url(args.url)
    settings = build_settings(args)
    settings.validate()

    async with ReviewSession(settings) as session:
        outcome = await session.run(
            owner,
            repo,
            number,
            post=args.post,
            skip_if_reviewed=not args.force,
            incremental=False if args.full else None,
        )

    if args.json:
        print(to_json(outcome))
    else:
        print(
            render(
                outcome, color=sys.stdout.isatty() and not args.no_color, show_metrics=args.metrics
            )
        )

    if args.fail_on == "never":
        return 0
    if args.fail_on == "any":
        return 1 if outcome.result.findings else 0
    return 1 if outcome.result.has_blocking else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-review", description="AI code review for a GitHub pull request"
    )
    parser.add_argument("url", help="GitHub pull request URL")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--post",
        action="store_true",
        help="publish the review to the PR (needs a token with pull_requests: write)",
    )
    parser.add_argument(
        "--force", action="store_true", help="post even if this commit was already reviewed"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="review the whole PR even if we reviewed an earlier commit of it",
    )
    parser.add_argument(
        "--request-changes",
        action="store_true",
        help="post as REQUEST_CHANGES when something critical is found",
    )
    parser.add_argument("--provider", choices=["gemini", "ollama"], help="override LLM_PROVIDER")
    parser.add_argument("--model", help="override the model for the chosen provider")
    parser.add_argument("--max-files", type=int, help="override MAX_FILES")
    parser.add_argument(
        "--no-static", action="store_true", help="skip the static analysis pre-pass"
    )
    parser.add_argument(
        "--no-context", action="store_true", help="send diffs only, without whole-file context"
    )
    parser.add_argument(
        "--repo-context",
        action="store_true",
        help="also pull in callers of the changed functions (costs code-search requests)",
    )
    parser.add_argument("--metrics", action="store_true", help="print the metrics block")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--fail-on",
        choices=["critical", "any", "never"],
        default="critical",
        help="exit non-zero when findings at this level exist (default: critical)",
    )
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else get_settings().log_level)

    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except (GitHubError, LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(3)
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary
        if args.verbose:
            raise
        print(f"unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("re-run with --verbose for a traceback", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
