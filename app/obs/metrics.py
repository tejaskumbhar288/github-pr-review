"""What a single review is worth measuring by.

Drop rate is the interesting one. Every finding the model produces that fails
anchor validation is a hallucination we caught, so ``dropped / proposed`` is a
concrete, per-review hallucination signal. That turns prompt changes into
something you can evaluate instead of vibe.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReviewMetrics:
    pr: str = ""
    head_sha: str = ""
    provider: str = ""
    model: str = ""

    files_reviewed: int = 0
    files_excluded: int = 0
    context_files: int = 0
    context_chars: int = 0
    static_findings: int = 0
    static_suppressed: int = 0
    """Lint findings dropped as structurally irrelevant to test code."""
    repo_context_files: int = 0
    """Files pulled in because they call something this PR changed."""
    repo_context_searches: int = 0

    prompt_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    llm_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    attempts: int = 1

    proposed_findings: int = 0
    """Findings the model returned, before validation."""
    kept_findings: int = 0
    dropped_findings: int = 0
    snapped_findings: int = 0
    """Findings rescued by snapping a near-miss line onto a real anchor."""
    duplicate_findings: int = 0

    critical: int = 0
    major: int = 0
    minor: int = 0

    error: str | None = None
    drop_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def drop_rate(self) -> float:
        """Fraction of proposed findings that failed validation. Lower is better."""
        if not self.proposed_findings:
            return 0.0
        return self.dropped_findings / self.proposed_findings

    def as_dict(self) -> dict[str, object]:
        return {
            "pr": self.pr,
            "head_sha": self.head_sha[:12],
            "provider": self.provider,
            "model": self.model,
            "files_reviewed": self.files_reviewed,
            "files_excluded": self.files_excluded,
            "context_files": self.context_files,
            "context_chars": self.context_chars,
            "static_findings": self.static_findings,
            "static_suppressed": self.static_suppressed,
            "repo_context_files": self.repo_context_files,
            "repo_context_searches": self.repo_context_searches,
            "prompt_chars": self.prompt_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_latency_ms": round(self.llm_latency_ms, 1),
            "total_latency_ms": round(self.total_latency_ms, 1),
            "attempts": self.attempts,
            "proposed_findings": self.proposed_findings,
            "kept_findings": self.kept_findings,
            "dropped_findings": self.dropped_findings,
            "snapped_findings": self.snapped_findings,
            "duplicate_findings": self.duplicate_findings,
            "drop_rate": round(self.drop_rate, 4),
            "critical": self.critical,
            "major": self.major,
            "minor": self.minor,
            "drop_reasons": self.drop_reasons,
            "error": self.error,
        }
