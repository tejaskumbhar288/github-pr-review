"""Prompt construction for the review pass.

Design notes worth defending:
  - The model is told to stay silent rather than pad. Reviewer credibility dies
    on nitpicks, so an empty findings list is an explicitly valid answer.
  - Line numbers come pre-computed in the diff; the model copies, never counts.
  - Style/formatting is out of scope because linters already own that, and
    whatever the linters *did* find is handed over as known issues so the model
    spends its attention on what tools structurally cannot catch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .analysis import StaticFinding, render_known_issues
from .repo_context import RepoContext

if TYPE_CHECKING:  # pragma: no cover
    from ..github.client import PullRequest

SYSTEM = """You are a senior software engineer reviewing a pull request.

Report only findings a thoughtful human reviewer would actually raise:
  - Correctness bugs, logic errors, off-by-one, wrong operator or condition
  - Unhandled edge cases: null/empty input, boundaries, concurrent access
  - Resource issues: unclosed handles, leaks, unbounded growth
  - Security: injection, missing authz, secrets in code, unsafe deserialization
  - Error handling: swallowed exceptions, failures that vanish silently
  - Clear performance traps: N+1 queries, work inside hot loops
  - API contract breaks that callers won't notice until runtime

Do NOT report:
  - Formatting, naming style, import order, line length (linters own these)
  - Anything already listed under "Known issues" - those are handled
  - Missing tests or docs as a generic complaint
  - Speculation about code you cannot see
  - Restatements of what the code obviously does

Anchoring rules (these decide whether a finding survives):
  - The diff is annotated as `LINE | diff-line`. Only lines with a number in
    that left column can be commented on. Copy the number exactly.
  - A blank left column means a deleted or structural line. Never anchor there.
  - Anchor to the line where the problem *is*, not the top of the function.
  - The `file` field must be the exact path from the "### " heading.

Judgement:
  - Ground each finding in the actual code, not in general best practice.
  - If the diff is clean, return an empty findings array. This is a good outcome
    and is strongly preferred over padding with speculation.
  - Prefer five sharp findings over twenty shallow ones. Never exceed 15.
  - Set `confidence` between 0 and 1. Below 0.5 means you are guessing - and if
    you are guessing, prefer to omit the finding entirely.

Severity:
  - critical: data loss, security hole, crash, or corruption in normal use
  - major: incorrect behaviour on a realistic input, or a resource leak
  - minor: a real but low-impact issue worth mentioning once

Return JSON only, matching this shape exactly:
{
  "summary": "two or three sentences on what this PR does and its overall risk",
  "findings": [
    {
      "file": "exact/path/from/the/diff.py",
      "line": 42,
      "severity": "critical" | "major" | "minor",
      "category": "correctness" | "security" | "performance" | "error-handling" | "maintainability",
      "title": "short problem statement, under 10 words",
      "detail": "why this is a problem and what breaks in practice",
      "suggestion": "concrete replacement code, or null if not applicable",
      "confidence": 0.85
    }
  ]
}"""


def build_user_prompt(
    pr: PullRequest,
    file_context: dict[str, str] | None = None,
    known_issues: list[StaticFinding] | None = None,
    repo_context: RepoContext | None = None,
) -> str:
    parts: list[str] = [
        f"# Pull request: {pr.title}",
        f"Repository: {pr.owner}/{pr.repo}  (PR #{pr.number})",
    ]

    if pr.body.strip():
        parts.append(f"\n## Author's description\n{pr.body.strip()}")

    if known_issues:
        parts.append(
            "\n## Known issues (already reported by static analysis)\n"
            "These are handled. Do NOT repeat them. They are shown so you can "
            "spend your attention on what linters cannot detect - intent, edge "
            "cases, and reasoning that spans functions.\n"
            f"```\n{render_known_issues(known_issues)}\n```"
        )

    if pr.is_incremental:
        # Said plainly, because the model would otherwise report the absence of
        # things it cannot see - "this function is never called", "no tests were
        # added" - about code that is simply outside the increment.
        parts.append(
            f"\n## Scope: incremental review\n"
            f"This PR was already reviewed at an earlier commit. The diff below "
            f"covers ONLY the {pr.new_commits} commit(s) pushed since then, not the "
            f"whole pull request. Do not comment on the absence of anything that "
            f"may already exist in the part of the PR you cannot see here."
        )

    parts.append(
        "\n## Changed files (diff annotated with new-file line numbers)\n"
        "Format is `LINE | diff-line`. A blank LINE means a deleted or structural "
        "line you must not anchor a comment to."
    )

    for f in pr.files:
        header = f"\n### {f.path} ({f.status}, +{f.additions}/-{f.deletions})"
        if f.previous_path:
            header += f"\nRenamed from `{f.previous_path}`."
        if f.patch.is_empty:
            parts.append(f"{header}\nNo textual diff available (binary or too large).")
            continue
        if f.patch.truncated:
            header += "\nNote: this diff was truncated; later hunks are not shown."
        parts.append(f"{header}\n```diff\n{f.patch.annotated}\n```")

    if file_context:
        parts.append(
            "\n## Full contents of touched files (for surrounding context)\n"
            "Use this to judge whether the change is correct in situ - what calls "
            "it, what invariants it relies on. Do NOT comment on lines the PR did "
            "not modify; they are context only and are not anchorable."
        )
        for path, content in file_context.items():
            parts.append(f"\n### {path} (full file at HEAD)\n```\n{content}\n```")

    if repo_context is not None and repo_context.call_sites:
        parts.append(
            "\n## Callers elsewhere in the repo\n"
            f"Code outside this PR that references "
            f"{', '.join(f'`{s}`' for s in repo_context.symbols)}. Use it to judge "
            "whether the change breaks an existing caller - a changed signature, a "
            "return that can now be None, an inverted condition. These excerpts are "
            "context only: they are not part of the diff and cannot be commented on, "
            "so anchor any finding they lead you to on the changed line that causes "
            "it."
        )
        parts.append(repo_context.render())

    if pr.truncated_files:
        parts.append(
            f"\n_Note: {pr.truncated_files} further file(s) were excluded from this "
            "review as generated, binary, or over the size budget._"
        )

    parts.append("\nReview the changes now and return the JSON object.")
    return "\n".join(parts)
