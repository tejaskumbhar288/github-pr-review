"""Unified-diff parsing.

This is the unglamorous piece that decides whether the tool feels real. An LLM
handed a bare diff will invent line numbers, and GitHub rejects a review
comment pointing at a line that isn't part of the diff. So we:

  1. annotate every diff line with its true line number in the new file,
  2. record exactly which line numbers are legal comment anchors, and
  3. keep each anchor's diff *position* for the legacy comment API,

then validate the model's output against (2) before posting anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)

# Marker for a line the model must not anchor to (deleted / structural).
NO_ANCHOR = " " * 6


@dataclass
class Hunk:
    old_start: int
    new_start: int
    header: str
    new_end: int = 0


@dataclass
class ParsedPatch:
    annotated: str = ""
    """Diff text with `new-file line number | original diff line` prefixes."""

    commentable: set[int] = field(default_factory=set)
    """Line numbers in the new file that GitHub will accept a comment on."""

    positions: dict[int, int] = field(default_factory=dict)
    """new-file line number -> position within the patch (legacy comment API)."""

    line_content: dict[int, str] = field(default_factory=dict)
    """new-file line number -> the line's text, without the diff marker."""

    added_lines: set[int] = field(default_factory=set)
    """Lines the PR actually introduced, as opposed to surrounding context."""

    hunks: list[Hunk] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.annotated

    def nearest_anchor(self, line: int, *, max_distance: int = 3) -> int | None:
        """Snap a near-miss line number onto a real anchor.

        Models are reliable about *which* code is wrong and occasionally off by
        one about where it starts - typically pointing at a function's `def`
        when the bug is in its first statement. Snapping within a tight radius
        recovers those findings; anything further away is a real hallucination
        and stays dropped.
        """
        if line in self.commentable:
            return line
        candidates = [c for c in self.commentable if abs(c - line) <= max_distance]
        if not candidates:
            return None
        # Prefer added lines over context, then proximity, then earlier lines.
        return min(candidates, key=lambda c: (c not in self.added_lines, abs(c - line), c))


def parse_patch(patch: str | None, *, max_lines: int | None = None) -> ParsedPatch:
    if not patch:
        return ParsedPatch()

    out: list[str] = []
    result = ParsedPatch()
    new_line = 0
    position = 0
    current: Hunk | None = None
    in_hunk = False

    for raw in patch.splitlines():
        position += 1
        if max_lines is not None and position > max_lines:
            result.truncated = True
            out.append(f"{NO_ANCHOR} | ... diff truncated at {max_lines} lines ...")
            break

        hunk_match = HUNK_RE.match(raw)
        if hunk_match:
            new_line = int(hunk_match.group("new"))
            current = Hunk(
                old_start=int(hunk_match.group("old")),
                new_start=new_line,
                header=raw,
            )
            result.hunks.append(current)
            in_hunk = True
            out.append(f"{NO_ANCHOR} | {raw}")
            continue

        if not in_hunk:
            # Preamble ("diff --git", "index ...", "--- a/x", "+++ b/x").
            out.append(f"{NO_ANCHOR} | {raw}")
            continue

        marker = raw[0] if raw else " "
        body = raw[1:] if raw else ""

        if marker == "+":
            result.commentable.add(new_line)
            result.added_lines.add(new_line)
            result.positions[new_line] = position
            result.line_content[new_line] = body
            result.added += 1
            out.append(f"{new_line:>6} | {raw}")
            if current:
                current.new_end = new_line
            new_line += 1
        elif marker == "-":
            result.removed += 1
            # Deleted lines have no new-file number; they are context for the
            # model but never a legal RIGHT-side anchor.
            out.append(f"{NO_ANCHOR} | {raw}")
        elif marker == "\\":
            # "\ No newline at end of file" - metadata, not a line of code.
            out.append(f"{NO_ANCHOR} | {raw}")
        else:
            result.commentable.add(new_line)
            result.positions[new_line] = position
            result.line_content[new_line] = body
            out.append(f"{new_line:>6} | {raw}")
            if current:
                current.new_end = new_line
            new_line += 1

    result.annotated = "\n".join(out)
    return result
