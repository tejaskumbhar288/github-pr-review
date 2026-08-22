from __future__ import annotations

import pytest

from app.review.analysis import StaticAnalyzer, StaticFinding, render_known_issues


def _find_ruff():
    from app.config import Settings

    return StaticAnalyzer(Settings.from_env({})).resolve("ruff")


RUFF = _find_ruff()
needs_ruff = pytest.mark.skipif(RUFF is None, reason="ruff is not installed")


async def test_disabled_analysis_returns_nothing(settings, pr):
    analyzer = StaticAnalyzer(settings.with_overrides(static_analysis=False))
    assert await analyzer.analyze(pr, {"src/io.py": "import os\n"}) == []


async def test_missing_tools_degrade_quietly(settings, pr):
    analyzer = StaticAnalyzer(
        settings.with_overrides(
            static_analysis=True, ruff_path="/nonexistent/ruff", semgrep_path="/nonexistent/semgrep"
        )
    )
    assert await analyzer.analyze(pr, {"src/io.py": "import os\n"}) == []


def test_tool_resolution_finds_ruff_in_the_venv(settings):
    """Regression: the pre-pass silently vanished when the venv was not activated."""
    assert StaticAnalyzer(settings).resolve("ruff") is not None
    assert StaticAnalyzer(settings).resolve("definitely-not-a-real-linter") is None


async def test_no_context_means_no_analysis(settings, pr):
    assert await StaticAnalyzer(settings).analyze(pr, {}) == []


@needs_ruff
async def test_ruff_findings_are_limited_to_lines_in_the_diff(settings, pr):
    source = (
        "import os\n"  # 1: unused import, in the diff
        "\n"  # 2
        "def load(path):\n"  # 3
        "    with open(path) as fh:\n"
        "        return fh.read()\n"
        "\n"
        "def save(path, data):\n"
        '    open(path, "w").write(data)\n'  # 8
        "import sys\n"  # 9: unused import, OUTSIDE the diff
    )
    analyzer = StaticAnalyzer(
        settings.with_overrides(static_analysis=True, ruff_path=RUFF, semgrep_path="/nope")
    )
    findings = await analyzer.analyze(pr, {"src/io.py": source})

    assert findings, "ruff should flag the unused import on line 1"
    assert all(f.tool == "ruff" for f in findings)
    assert all(f.file == "src/io.py" for f in findings)
    # Line 9 is past the end of the diff, so it must have been filtered out.
    assert all(f.line in pr.files[0].patch.commentable for f in findings)
    assert not any(f.line == 9 for f in findings)


@needs_ruff
async def test_findings_are_capped(settings, pr):
    source = "\n".join(f"import mod{i}" for i in range(8))
    analyzer = StaticAnalyzer(
        settings.with_overrides(
            static_analysis=True, ruff_path=RUFF, semgrep_path="/nope", max_known_issues=2
        )
    )
    findings = await analyzer.analyze(pr, {"src/io.py": source})
    assert len(findings) <= 2


async def test_path_traversal_in_a_filename_is_refused(settings, pr, tmp_path):
    """A malicious PR must not be able to write outside the sandbox."""
    from app.review.analysis import _materialise

    written = _materialise(tmp_path, {"../escape.py": "x = 1", "ok.py": "y = 2"})
    assert written == ["ok.py"]
    assert not (tmp_path.parent / "escape.py").exists()


def test_known_issues_render_compactly():
    out = render_known_issues(
        [
            StaticFinding("ruff", "a.py", 3, "F401", "unused import"),
            StaticFinding("semgrep", "b.py", 9, "sqli", "tainted query"),
        ]
    )
    assert out == "a.py:3 [ruff:F401] unused import\nb.py:9 [semgrep:sqli] tainted query"


def test_known_issues_reach_the_prompt(pr):
    from app.review.prompt import build_user_prompt

    prompt = build_user_prompt(
        pr, None, [StaticFinding("ruff", "src/io.py", 1, "F401", "unused import `os`")]
    )
    assert "Known issues" in prompt
    assert "unused import `os`" in prompt
    assert "Do NOT repeat them" in prompt


def test_prompt_omits_the_section_when_there_are_no_known_issues(pr):
    from app.review.prompt import build_user_prompt

    assert "Known issues" not in build_user_prompt(pr, None, [])


async def test_the_semgrep_config_default_is_compatible_with_metrics_off(settings, pr, monkeypatch):
    """Regression: the shipped default was `--config auto`, which semgrep
    refuses whenever metrics are off - and the pre-pass hardcodes `--metrics
    off`, because phoning the registry home about proprietary code is the exact
    thing the local path exists to avoid. Every semgrep run failed with
    "Cannot create auto config when metrics are off", the error was swallowed as
    a degraded tool, and the pre-pass silently returned ruff findings only.
    """
    import sys

    captured: list[list[str]] = []

    async def fake_exec(self, cmd, *, cwd):
        captured.append(cmd)

    monkeypatch.setattr(StaticAnalyzer, "_exec", fake_exec)
    # Any real file resolves as "the semgrep binary"; the command is what matters.
    analyzer = StaticAnalyzer(
        settings.with_overrides(static_analysis=True, semgrep_path=sys.executable)
    )
    await analyzer.analyze(pr, {"src/io.py": "import os\n"})

    (semgrep_cmd,) = [c for c in captured if c[0] == sys.executable]
    assert "--metrics" in semgrep_cmd and semgrep_cmd[semgrep_cmd.index("--metrics") + 1] == "off"
    assert semgrep_cmd[semgrep_cmd.index("--config") + 1] != "auto"
