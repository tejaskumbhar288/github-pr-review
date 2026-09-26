"""Static analysis pre-pass (roadmap 5).

Linters are cheap, deterministic and already correct about the things they
cover. Running them first and handing the results to the model as *known
issues* stops the model spending attention re-deriving what ruff found in
50ms, and lets the prompt tell it to look for what tools structurally cannot
catch: intent, edge cases, and cross-function reasoning.

Everything here is best-effort. A missing binary, a timeout or unparseable
output degrades to "no known issues" - never to a failed review.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..github.client import PullRequest

log = logging.getLogger(__name__)

PY_SUFFIXES = {".py", ".pyi"}

# --- test-path noise ------------------------------------------------------

# Rules that are correct in general and wrong in a test file. The pre-pass runs
# `--isolated` deliberately: the PR's own ruff config is not available here, so
# whatever per-directory ignores the project has set are invisible to us. That
# trade is right - most repos have no config we could read anyway - but it means
# the one thing nearly every Python project silences under `tests/` arrives at
# full volume.
#
# Measured, not guessed: of 14 static findings on the first real review this
# tool posted, ten were `S101 Use of assert detected` on test files. `assert` is
# how pytest works. Ten of fourteen slots spent telling a maintainer that their
# tests contain assertions is how a review bot gets muted in its second week.
#
# Each entry is a rule that is *structurally* inapplicable to test code, not one
# that merely fires often there. The distinction is the whole safety argument:
# suppressing a rule that could still catch a real bug in a test trades noise
# for silence, which is the worse failure and the harder one to notice.
TEST_PATH_IGNORES = frozenset(
    {
        # pytest's entire assertion model. Not a finding; a language feature.
        "S101",
        # Fixtures are injected by name and are routinely unused in the body.
        # A test signature is a request for setup, not a parameter list.
        "ARG001",
        "ARG002",
        "ARG005",
        # Credentials in tests are fakes by construction. A real secret checked
        # into a test is a secret-scanning problem - a different tool, with a
        # different confidence level and a different response.
        "S105",
        "S106",
        "S107",
    }
)

# Path shapes that mean "test" across the ecosystems this reviewer sees. Listed
# explicitly rather than folded into one clever regex, because a false positive
# here silences a real finding on production code.
_TEST_DIR_PARTS = frozenset(
    {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e", "integration_tests"}
)
_TEST_FILE_PREFIXES = ("test_", "spec_")
_TEST_FILE_INFIXES = (".test.", ".spec.", "_test.", "_spec.")


def is_test_path(path: str) -> bool:
    """Is this file test code, rather than the code under test?

    Conservative on purpose. `src/testing_utils.py` is production code that
    happens to start with the right letters, so a prefix has to be followed by
    something - and the directory check only looks at parent components, never
    at the file name itself.
    """
    if not path:
        return False
    parts = path.replace("\\", "/").lstrip("./").split("/")
    name = parts[-1]

    if any(part.lower() in _TEST_DIR_PARTS for part in parts[:-1]):
        return True
    if any(infix in name for infix in _TEST_FILE_INFIXES):
        return True
    stem = name.rsplit(".", 1)[0]
    if stem == "conftest":
        return True
    return any(
        stem.startswith(prefix) and len(stem) > len(prefix) for prefix in _TEST_FILE_PREFIXES
    )


def is_known_irrelevant(finding: StaticFinding) -> bool:
    """Would a human reviewer delete this finding without reading the code?"""
    return is_test_path(finding.file) and finding.code in TEST_PATH_IGNORES


@dataclass(frozen=True)
class StaticFinding:
    tool: str
    file: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.file}:{self.line} [{self.tool}:{self.code}] {self.message}"


class StaticAnalyzer:
    """Runs available linters over the post-change contents of touched files."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self.suppressed_count = 0
        """Findings dropped by the test-path suppression list on the last run."""

    def resolve(self, name: str) -> str | None:
        """Find a linter on PATH, or failing that inside the running venv.

        ``pip install -r requirements-dev.txt`` puts ruff in the venv's bin
        directory, which is only on PATH when the venv is activated. Reviews
        run via ``python -m app.cli`` often are not, and silently losing the
        pre-pass because of that is a confusing way to lose quality.
        """
        found = shutil.which(name)
        if found:
            return found
        # An explicit path that does not resolve is a configuration error, not
        # an invitation to run some other binary that happens to share a name.
        if "/" in name or "\\" in name:
            return None
        for base in (Path(sys.prefix), Path(sys.executable).parent.parent):
            for candidate in (base / "bin" / name, base / "Scripts" / f"{name}.exe"):
                if candidate.is_file():
                    return str(candidate)
        return None

    def available_tools(self) -> dict[str, str]:
        found = {}
        for label, configured in (
            ("ruff", self._settings.ruff_path),
            ("semgrep", self._settings.semgrep_path),
        ):
            path = self.resolve(configured)
            if path:
                found[label] = path
        return found

    async def analyze(self, pr: PullRequest, file_contents: dict[str, str]) -> list[StaticFinding]:
        self.suppressed_count = 0
        if not self._settings.static_analysis or not file_contents:
            return []
        tools = self.available_tools()
        if not tools:
            log.debug("no static analysis tools on PATH; skipping pre-pass")
            return []

        with tempfile.TemporaryDirectory(prefix="ai-review-") as tmp:
            root = Path(tmp)
            written = _materialise(root, file_contents)
            if not written:
                return []

            jobs = []
            if "ruff" in tools and any(Path(p).suffix in PY_SUFFIXES for p in written):
                jobs.append(self._run_ruff(root, tools["ruff"]))
            if "semgrep" in tools:
                jobs.append(self._run_semgrep(root, tools["semgrep"]))

            results = await asyncio.gather(*jobs, return_exceptions=True)

        findings: list[StaticFinding] = []
        for res in results:
            if isinstance(res, BaseException):
                log.debug("static analysis job failed: %s", res)
                continue
            findings.extend(res)

        return self._filter_to_diff(pr, findings)

    # --- tool runners -----------------------------------------------------

    async def _run_ruff(self, root: Path, binary: str) -> list[StaticFinding]:
        out = await self._exec(
            [
                binary,
                "check",
                "--output-format",
                "json",
                "--no-cache",
                # The PR's own config isn't available here, so use a broad but
                # low-noise default set rather than whatever ruff ships today.
                "--select",
                "E,F,W,B,S,A,C4,T20,SIM,RET,ARG,PTH,ASYNC",
                "--isolated",
                ".",
            ],
            cwd=root,
        )
        if not out:
            return []
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return []
        findings = []
        for item in items if isinstance(items, list) else []:
            loc = item.get("location") or {}
            findings.append(
                StaticFinding(
                    tool="ruff",
                    file=_relpath(item.get("filename", ""), root),
                    line=int(loc.get("row") or 0),
                    code=str(item.get("code") or "?"),
                    message=str(item.get("message") or "").strip(),
                )
            )
        return findings

    async def _run_semgrep(self, root: Path, binary: str) -> list[StaticFinding]:
        out = await self._exec(
            [
                binary,
                "scan",
                "--json",
                "--quiet",
                "--config",
                self._settings.semgrep_config,
                "--metrics",
                "off",
                "--disable-version-check",
                ".",
            ],
            cwd=root,
        )
        if not out:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        findings = []
        for item in data.get("results") or []:
            extra = item.get("extra") or {}
            findings.append(
                StaticFinding(
                    tool="semgrep",
                    file=_relpath(item.get("path", ""), root),
                    line=int((item.get("start") or {}).get("line") or 0),
                    code=str(item.get("check_id") or "?").rsplit(".", 1)[-1],
                    message=str(extra.get("message") or "").strip().replace("\n", " "),
                )
            )
        return findings

    async def _exec(self, cmd: list[str], *, cwd: Path) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            log.debug("cannot launch %s: %s", cmd[0], exc)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._settings.static_analysis_timeout
            )
        except TimeoutError:
            log.warning("%s timed out after %.0fs", cmd[0], self._settings.static_analysis_timeout)
            proc.kill()
            await proc.wait()
            return None

        # Linters exit non-zero *because* they found something; that is success.
        if stderr and not stdout:
            log.debug("%s: %s", cmd[0], stderr.decode("utf-8", "replace")[:300])
        return stdout.decode("utf-8", "replace") if stdout else None

    # --- relevance --------------------------------------------------------

    def _filter_to_diff(
        self, pr: PullRequest, findings: list[StaticFinding]
    ) -> list[StaticFinding]:
        """Keep only what the PR is responsible for.

        A pre-existing lint error three hundred lines away is not this PR's
        problem, and feeding it in would just teach the model to comment on it.
        """
        kept: list[StaticFinding] = []
        seen: set[tuple[str, int, str]] = set()
        suppressed = 0
        for f in findings:
            pr_file = pr.file(f.file)
            if pr_file is None or f.line not in pr_file.patch.commentable:
                continue
            if self._settings.suppress_test_noise and is_known_irrelevant(f):
                suppressed += 1
                continue
            key = (f.file, f.line, f.code)
            if key in seen:
                continue
            seen.add(key)
            kept.append(f)

        # Counted and logged rather than silently discarded. A suppression list
        # is a claim about what does not matter, and a wrong claim is invisible
        # unless the number it removes is visible somewhere.
        self.suppressed_count = suppressed
        if suppressed:
            log.info("suppressed %d known-irrelevant static finding(s) on test paths", suppressed)

        kept.sort(key=lambda f: (f.file, f.line, f.code))
        limit = self._settings.max_known_issues
        if limit and len(kept) > limit:
            log.debug("truncating %d static findings to %d", len(kept), limit)
            kept = kept[:limit]
        return kept


def _materialise(root: Path, file_contents: dict[str, str]) -> list[str]:
    """Write the touched files into a temp tree, preserving relative paths."""
    written: list[str] = []
    for path, content in file_contents.items():
        target = (root / path).resolve()
        # Refuse anything that would escape the sandbox via .. or an absolute path.
        if not str(target).startswith(str(root.resolve()) + "/"):
            log.warning("skipping suspicious path in static analysis: %s", path)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            log.debug("cannot materialise %s: %s", path, exc)
            continue
        written.append(path)
    return written


def _relpath(raw: str, root: Path) -> str:
    if not raw:
        return ""
    path = Path(raw)
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return raw.lstrip("./")


def render_known_issues(findings: list[StaticFinding], limit: int = 60) -> str:
    lines = [f.render() for f in findings[:limit]]
    return "\n".join(lines)
