"""Eval case definitions (roadmap 6).

A case is a pull request with bugs we already know about, plus the assertion
that a good review finds them. Cases come in two flavours:

  - **fixture**: a hand-written diff stored on disk. Deterministic, offline, and
    fast, so it can run in CI on every prompt change.
  - **live**: a real PR URL. Slower, rate-limited and mutable, but the only
    thing that measures behaviour on real-world code.

Both score identically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}


@dataclass
class ExpectedBug:
    """A bug we assert the reviewer should find."""

    file: str
    line: int
    description: str = ""
    window: int = 6
    """How far from `line` a finding may anchor and still count as a hit.

    Reviewers legitimately disagree about whether a bug lives on the line that
    computes a bad value or the line that uses it. A tight window keeps that
    from being scored as a miss without letting a finding across the file count.
    """
    keywords: list[str] = field(default_factory=list)
    """At least one must appear in the finding's title or detail, if given."""
    min_severity: str = "minor"

    def matches(self, file: str, line: int, text: str, severity: str) -> bool:
        if file != self.file:
            return False
        if abs(line - self.line) > self.window:
            return False
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(self.min_severity, 0):
            return False
        if self.keywords:
            lowered = text.lower()
            return any(k.lower() in lowered for k in self.keywords)
        return True


@dataclass
class EvalCase:
    name: str
    expected: list[ExpectedBug] = field(default_factory=list)
    url: str | None = None
    fixture: str | None = None
    description: str = ""
    forbidden_files: list[str] = field(default_factory=list)
    """Files a finding must never anchor to - e.g. untouched vendored code."""

    @property
    def kind(self) -> str:
        return "fixture" if self.fixture else "live"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        return cls(
            name=data["name"],
            url=data.get("url"),
            fixture=data.get("fixture"),
            description=data.get("description", ""),
            forbidden_files=list(data.get("forbidden_files") or []),
            expected=[
                ExpectedBug(
                    file=e["file"],
                    line=int(e["line"]),
                    description=e.get("description", ""),
                    window=int(e.get("window", 6)),
                    keywords=list(e.get("keywords") or []),
                    min_severity=e.get("min_severity", "minor"),
                )
                for e in data.get("expected") or []
            ],
        )


def load_cases(path: str | Path) -> list[EvalCase]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no eval case file at {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    return [EvalCase.from_dict(c) for c in cases]


def load_fixture_pr(path: str | Path, base_dir: Path | None = None):
    """Build a PullRequest from a fixture file, with no network access."""
    from ..github.client import PRFile, PullRequest
    from ..github.patch import parse_patch

    p = Path(path)
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    data = json.loads(p.read_text(encoding="utf-8"))

    files = [
        PRFile(
            path=f["path"],
            status=f.get("status", "modified"),
            additions=int(f.get("additions") or 0),
            deletions=int(f.get("deletions") or 0),
            patch=parse_patch(f["patch"]),
        )
        for f in data.get("files") or []
    ]
    contents = {f["path"]: f["content"] for f in data.get("files") or [] if f.get("content")}

    pr = PullRequest(
        owner=data.get("owner", "fixture"),
        repo=data.get("repo", "fixture"),
        number=int(data.get("number") or 1),
        title=data.get("title", ""),
        body=data.get("body", ""),
        base_sha=data.get("base_sha", "base"),
        head_sha=data.get("head_sha", "head"),
        files=files,
    )
    return pr, contents
