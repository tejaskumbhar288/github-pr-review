from __future__ import annotations

import pytest

from app.review.engine import ReviewEngine
from tests.conftest import FakeGitHub, FakeProvider, finding_payload


def build(settings, pr, payload):
    gh = FakeGitHub(pr)
    llm = FakeProvider(payload)
    return ReviewEngine(gh, llm, settings), gh, llm


async def review(settings, pr, payload):
    engine, _, _ = build(settings, pr, payload)
    return await engine.review_pr(pr)


async def test_valid_finding_survives(settings, pr):
    result = await review(settings, pr, {"summary": "s", "findings": [finding_payload()]})
    assert len(result.findings) == 1
    assert result.findings[0].line == 8
    assert not result.dropped
    assert result.metrics.drop_rate == 0.0


async def test_unknown_file_is_dropped(settings, pr):
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(file="nope/ghost.py")]}
    )
    assert not result.findings
    assert "unknown file" in result.dropped[0]
    assert result.metrics.drop_reasons == {"unknown_file": 1}
    assert result.metrics.drop_rate == 1.0


async def test_line_outside_the_diff_is_dropped(settings, pr):
    result = await review(settings, pr, {"summary": "s", "findings": [finding_payload(line=9999)]})
    assert not result.findings
    assert "not part of the diff" in result.dropped[0]


async def test_near_miss_line_is_snapped_and_counted(settings, pr):
    """A finding one line off is recovered, and the rescue is recorded."""
    result = await review(settings, pr, {"summary": "s", "findings": [finding_payload(line=10)]})
    assert len(result.findings) == 1
    assert result.findings[0].line == 8
    assert result.findings[0].snapped_from == 10
    assert result.metrics.snapped_findings == 1


async def test_non_numeric_line_is_dropped(settings, pr):
    result = await review(settings, pr, {"summary": "s", "findings": [finding_payload(line="ten")]})
    assert not result.findings and "non-numeric" in result.dropped[0]


async def test_empty_title_is_dropped(settings, pr):
    result = await review(settings, pr, {"summary": "s", "findings": [finding_payload(title="  ")]})
    assert not result.findings and "empty title" in result.dropped[0]


async def test_low_confidence_is_dropped(settings, pr):
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(confidence=0.1)]}
    )
    assert not result.findings
    assert result.metrics.drop_reasons == {"low_confidence": 1}


async def test_missing_confidence_is_trusted(settings, pr):
    payload = finding_payload()
    del payload["confidence"]
    result = await review(settings, pr, {"summary": "s", "findings": [payload]})
    assert len(result.findings) == 1 and result.findings[0].confidence == 1.0


async def test_duplicate_findings_are_collapsed(settings, pr):
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(), finding_payload()]}
    )
    assert len(result.findings) == 1
    assert result.metrics.duplicate_findings == 1


async def test_unknown_severity_and_category_fall_back(settings, pr):
    result = await review(
        settings,
        pr,
        {"summary": "s", "findings": [finding_payload(severity="apocalyptic", category="vibes")]},
    )
    assert result.findings[0].severity == "minor"
    assert result.findings[0].category == "maintainability"


async def test_findings_sort_critical_first(settings, pr):
    payload = {
        "summary": "s",
        "findings": [
            finding_payload(line=8, severity="minor", title="c"),
            finding_payload(line=6, severity="critical", title="a"),
            finding_payload(line=7, severity="major", title="b"),
        ],
    }
    result = await review(settings, pr, payload)
    assert [f.severity for f in result.findings] == ["critical", "major", "minor"]
    assert result.counts == {"critical": 1, "major": 1, "minor": 1}
    assert result.has_blocking


async def test_silence_is_a_valid_answer(settings, pr):
    result = await review(settings, pr, {"summary": "Looks fine.", "findings": []})
    assert result.findings == [] and result.dropped == []
    assert result.summary == "Looks fine."


async def test_malformed_payload_does_not_crash(settings, pr):
    result = await review(settings, pr, {"summary": None, "findings": "not-a-list"})
    assert result.findings == []
    assert result.summary == "(no summary returned)"


async def test_non_object_finding_is_dropped(settings, pr):
    result = await review(settings, pr, {"summary": "s", "findings": ["oops", 42]})
    assert result.metrics.drop_reasons == {"malformed": 2}


async def test_null_suggestion_string_becomes_none(settings, pr):
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(suggestion="null")]}
    )
    assert result.findings[0].suggestion is None


async def test_leading_dot_slash_in_path_is_normalised(settings, pr):
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(file="./src/io.py")]}
    )
    assert len(result.findings) == 1


async def test_pr_with_no_reviewable_files_short_circuits(settings, pr):
    pr.files = []
    engine, _, llm = build(settings, pr, {"summary": "x", "findings": []})
    result = await engine.review_pr(pr)
    assert "No reviewable source changes" in result.summary
    assert llm.calls == []


async def test_metrics_capture_tokens_and_drop_rate(settings, pr):
    payload = {
        "summary": "s",
        "findings": [finding_payload(), finding_payload(line=9999, title="ghost")],
    }
    result = await review(settings, pr, payload)
    m = result.metrics
    assert m.proposed_findings == 2 and m.kept_findings == 1 and m.dropped_findings == 1
    assert m.drop_rate == 0.5
    assert m.prompt_tokens == 100 and m.completion_tokens == 20 and m.total_tokens == 120
    assert m.provider == "fake" and m.model == "fake-1"
    assert m.prompt_chars > 0


async def test_whole_file_context_is_fetched_and_included(settings, pr):
    settings = settings.with_overrides(context_char_limit=60_000)
    gh = FakeGitHub(pr, {"src/io.py": "def load(path):\n    ...\n"})
    llm = FakeProvider({"summary": "s", "findings": []})
    engine = ReviewEngine(gh, llm, settings)
    result = await engine.review_pr(pr)
    assert result.metrics.context_files == 1
    assert "full file at HEAD" in llm.calls[0]["user"]


async def test_oversized_files_are_sent_as_diff_only(settings, pr):
    settings = settings.with_overrides(context_char_limit=10)
    gh = FakeGitHub(pr, {"src/io.py": "x" * 500})
    llm = FakeProvider({"summary": "s", "findings": []})
    result = await ReviewEngine(gh, llm, settings).review_pr(pr)
    assert result.metrics.context_files == 0


async def test_llm_failure_still_records_metrics(settings, pr):
    class Boom(FakeProvider):
        async def complete_json(self, **kwargs):
            raise RuntimeError("provider exploded")

    recorded = []

    class Recorder:
        enabled = False

        def review(self, *a, **k):
            from contextlib import nullcontext

            from app.obs.tracing import Span

            return nullcontext(Span())

        def record(self, m):
            recorded.append(m)

        def flush(self):
            pass

    engine = ReviewEngine(FakeGitHub(pr), Boom(), settings, tracer=Recorder())
    with pytest.raises(RuntimeError, match="provider exploded"):
        await engine.review_pr(pr)
    assert recorded and "provider exploded" in recorded[0].error


# Captured verbatim from a real gemini-3.6-flash review. Two degeneration
# shapes showed up across two runs of the same PR, so both are pinned here.
SALAD = (
    "block sequence execution code step parameters configuration properties setup "
    "logic block details statement pattern structure definition logic code sample "
    "snippet logic configuration parameter setup structure specification definition "
    "statement sequence setup execution block pattern details statement code block "
    "logic definition pattern details setup sequence script snippet block statement "
    "structure context logic definitions setup parameters specification script "
    "snippet structure definition logic details block code sample sequence execution "
    "structure pattern logic definitions setup parameters context pattern code block "
    "structure logic setup configuration parameters specification logic script snip"
)


async def test_degenerate_repetition_in_a_suggestion_is_truncated(settings, pr):
    """Regression from the first real review: the model looped on one token for
    hundreds of words. A character cap alone still leaves the loop visible."""
    payload = finding_payload(suggestion="use a context manager: " + "context " * 300)
    result = await review(settings, pr, {"summary": "s", "findings": [payload]})
    suggestion = result.findings[0].suggestion
    assert suggestion == "use a context manager:"
    assert "context context" not in suggestion


async def test_a_wholly_degenerate_suggestion_is_dropped_but_the_finding_survives(settings, pr):
    payload = finding_payload(suggestion="x " * 50)
    result = await review(settings, pr, {"summary": "s", "findings": [payload]})
    assert len(result.findings) == 1, "the finding itself is still valuable"
    assert result.findings[0].suggestion is None


async def test_low_diversity_word_salad_is_truncated(settings, pr):
    """The second degeneration shape: no exact repeats, but a 0.29 diversity
    ratio over 80+ bare words. A character cap alone leaves the salad visible."""
    payload = finding_payload(suggestion=f"with SessionLocal() as db:\n    db.add(x)\n# {SALAD}")
    result = await review(settings, pr, {"summary": "s", "findings": [payload]})
    suggestion = result.findings[0].suggestion
    assert "with SessionLocal() as db:" in suggestion
    assert "specification" not in suggestion
    assert len(suggestion) < 120


async def test_repetitive_but_legitimate_code_is_not_mangled(settings, pr):
    """Schema definitions repeat heavily by nature; they must survive intact."""
    code = "\n".join(f"    field_{i} = Column(String, nullable=False)" for i in range(20))
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(suggestion=code)]}
    )
    assert result.findings[0].suggestion == code.strip()


async def test_ordinary_prose_advice_survives(settings, pr):
    """A borderline-length sentence of real advice must not trip the detector."""
    advice = (
        "Use a context manager here so the session is closed even when the rule "
        "check raises, and pass document_id explicitly rather than reading it off "
        "the extraction schema where it does not exist."
    )
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(suggestion=advice)]}
    )
    assert result.findings[0].suggestion == advice


async def test_legitimate_repetition_is_preserved(settings, pr):
    """Real code repeats short tokens; don't mangle it."""
    code = "a = 1\nb = 2\nc = 3"
    result = await review(
        settings, pr, {"summary": "s", "findings": [finding_payload(suggestion=code)]}
    )
    assert result.findings[0].suggestion == code


async def test_placeholder_suggestions_become_none(settings, pr):
    for placeholder in ("null", "none", "N/A", "  "):
        result = await review(
            settings,
            pr,
            {"summary": "s", "findings": [finding_payload(suggestion=placeholder)]},
        )
        assert result.findings[0].suggestion is None, placeholder


async def test_explicit_context_bypasses_the_network_fetch(settings, pr):
    """The eval harness needs offline context without patching a shared engine -
    monkeypatching one is not safe when cases run concurrently."""
    from app.review.engine import ReviewEngine
    from tests.conftest import FakeGitHub, FakeProvider

    gh = FakeGitHub(pr, {"src/io.py": "SHOULD NOT BE USED"})
    llm = FakeProvider({"summary": "s", "findings": []})
    engine = ReviewEngine(gh, llm, settings.with_overrides(context_char_limit=60_000))

    await engine.review_pr(pr, context={"src/io.py": "supplied by the caller"})
    assert "supplied by the caller" in llm.calls[0]["user"]
    assert "SHOULD NOT BE USED" not in llm.calls[0]["user"]


async def test_concurrent_reviews_on_one_engine_do_not_cross_contaminate(settings, pr):
    """The bug the eval harness hit: shared mutable state across concurrent cases."""
    import asyncio
    import copy

    from app.review.engine import ReviewEngine
    from tests.conftest import FakeGitHub, FakeProvider

    llm = FakeProvider({"summary": "s", "findings": []})
    engine = ReviewEngine(FakeGitHub(pr), llm, settings)

    prs = [copy.deepcopy(pr) for _ in range(4)]
    await asyncio.gather(
        *(engine.review_pr(p, context={"src/io.py": f"marker-{i}"}) for i, p in enumerate(prs))
    )

    seen = {f"marker-{i}" for i in range(4)}
    got = {m for m in seen if any(m in call["user"] for call in llm.calls)}
    assert got == seen, "each concurrent review must see only its own context"
