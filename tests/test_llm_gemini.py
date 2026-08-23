"""HTTP-level behaviour of the Gemini backend.

The happy path is covered indirectly through the engine tests; what matters
here are the failure modes, and above all the 429s - misclassifying one costs
real quota.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.llm.base import LLMError, RateLimited
from app.llm.gemini import BASE, MAX_OUTPUT_TOKENS, GeminiProvider

MODEL = "gemini-3.6-flash"
URL = f"{BASE}/models/{MODEL}:generateContent"


def quota_body(quota_id: str, *, retry_delay: str | None = "40s") -> dict:
    details: list[dict] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": quota_id,
                    "quotaDimensions": {"location": "global", "model": MODEL},
                    "quotaValue": "20",
                }
            ],
        }
    ]
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}


def ok_body(text: str = '{"summary": "fine", "findings": []}') -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
    }


async def complete(provider: GeminiProvider):
    return await provider.complete_json(system="s", user="u")


@pytest.fixture
def provider() -> GeminiProvider:
    return GeminiProvider(api_key="k", model=MODEL, max_retries=3)


@respx.mock
async def test_daily_quota_fails_immediately_without_burning_retries(provider):
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        )
    )

    with pytest.raises(LLMError, match="daily quota exhausted"):
        await complete(provider)

    # The whole point: one request, not max_retries of them.
    assert route.call_count == 1


@respx.mock
async def test_daily_quota_message_names_the_model_and_the_cap(provider):
    respx.post(URL).mock(
        return_value=httpx.Response(
            429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        )
    )

    with pytest.raises(LLMError) as exc:
        await complete(provider)

    assert MODEL in str(exc.value)
    assert "20/day" in str(exc.value)


@respx.mock
async def test_per_minute_quota_is_retried_and_can_succeed(provider):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                429, json=quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
            ),
            httpx.Response(200, json=ok_body()),
        ]
    )

    result = await complete(provider)

    assert result.attempts == 2
    assert result.data == {"summary": "fine", "findings": []}


@respx.mock
async def test_retry_after_header_wins_over_the_body(provider):
    seen: list[float | None] = []
    respx.post(URL).mock(
        return_value=httpx.Response(
            429,
            headers={"retry-after": "2"},
            json=quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
        )
    )

    with pytest.raises(LLMError):
        await _first_attempt(provider, seen)

    assert seen == [2.0]


@respx.mock
async def test_retry_info_is_used_when_no_header_is_sent(provider):
    seen: list[float | None] = []
    respx.post(URL).mock(
        return_value=httpx.Response(
            429,
            json=quota_body(
                "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", retry_delay="7s"
            ),
        )
    )

    with pytest.raises(LLMError):
        await _first_attempt(provider, seen)

    assert seen == [7.0]


@respx.mock
async def test_a_429_with_no_parseable_body_is_still_retryable(provider):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, text="<html>too many requests</html>"),
            httpx.Response(200, json=ok_body()),
        ]
    )

    result = await complete(provider)

    assert result.attempts == 2


@respx.mock
async def test_usage_and_model_metadata_come_back_with_the_payload(provider):
    respx.post(URL).mock(return_value=httpx.Response(200, json=ok_body()))

    result = await complete(provider)

    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 4
    assert result.usage.total_tokens == 15
    assert (result.provider, result.model) == ("gemini", MODEL)


@respx.mock
async def test_a_bad_key_is_not_retried(provider):
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "API key not valid"}})
    )

    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        await complete(provider)

    assert route.call_count == 1


@respx.mock
async def test_truncated_output_explains_the_knob_to_turn(provider):
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        )
    )

    with pytest.raises(LLMError, match="MAX_FILES"):
        await complete(provider)


@respx.mock
async def test_server_errors_are_retried(provider):
    respx.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=ok_body())])

    assert (await complete(provider)).attempts == 2


async def _first_attempt(provider: GeminiProvider, seen: list[float | None]) -> None:
    """Run one request and capture the retry hint the provider derived."""
    import app.llm.gemini as mod

    original = mod.with_backoff

    async def once(fn, **kw):
        try:
            await fn()
        except RateLimited as exc:
            seen.append(exc.retry_after)
            raise LLMError("captured") from exc
        raise AssertionError("expected a RateLimited")

    mod.with_backoff = once
    try:
        await provider.complete_json(system="s", user="u")
    finally:
        mod.with_backoff = original


@respx.mock
async def test_truncated_json_is_reported_as_truncation_not_as_bad_json(provider):
    """The failure mode that actually happens: MAX_TOKENS *with* partial text.

    The model emits most of its object before the limit bites, so the text is
    non-empty and looks parseable-ish. Reporting that as "did not return JSON"
    points at the wrong problem.
    """
    half = '{"summary": "long", "findings": [{"file": "io/exporte'
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": half}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
        )
    )

    with pytest.raises(LLMError, match="output limit mid-response"):
        await complete(provider)


@respx.mock
async def test_a_truncated_response_is_retried_rather_than_failing_the_review(provider):
    """Truncation is sampling variance, not a property of the request.

    Observed live: one attempt ran to answer=26383 tokens and was cut off; the
    identical prompt returned a complete 643-token review moments later. The
    extraction therefore has to happen inside the retried call - when it sat
    after the backoff, a single unlucky sample failed the whole review.
    """
    half = '{"summary": "long", "findings": [{"file": "a.py"'
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": half}]}, "finishReason": "MAX_TOKENS"}
                    ]
                },
            ),
            httpx.Response(200, json=ok_body()),
        ]
    )

    result = await complete(provider)

    assert route.call_count == 2
    assert result.attempts == 2
    assert result.data == {"summary": "fine", "findings": []}


@respx.mock
async def test_persistent_truncation_still_names_the_knobs_to_turn(provider):
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "{"}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
        )
    )

    with pytest.raises(LLMError, match="MAX_FILES"):
        await complete(provider)


@respx.mock
async def test_a_safety_block_with_partial_text_is_still_a_safety_block(provider):
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "I cannot"}]}, "finishReason": "SAFETY"}
                ]
            },
        )
    )

    with pytest.raises(LLMError, match="safety"):
        await complete(provider)


@respx.mock
async def test_the_truncation_error_says_where_the_budget_went(provider):
    """thinking=8100/answer=92 and answer=8192 need different fixes."""
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"summ'}]}, "finishReason": "MAX_TOKENS"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 1336,
                    "candidatesTokenCount": 6451,
                    "thoughtsTokenCount": 1725,
                },
            },
        )
    )

    with pytest.raises(LLMError) as exc:
        await complete(provider)

    assert "thinking=1725" in str(exc.value)
    assert "answer=6451" in str(exc.value)


@respx.mock
async def test_the_output_budget_is_sent_explicitly(provider):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_body()))

    await complete(provider)

    import json as _json

    sent = _json.loads(route.calls[0].request.content)
    assert sent["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS


@respx.mock
async def test_the_schema_carries_no_keyword_some_models_reject(provider):
    """maxItems looked like the right way to bound a runaway model. It is not:
    the lite models reject the whole request with a bare 400, and the schema has
    to be the one thing that works everywhere."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_body()))

    await complete(provider)

    import json as _json

    schema = _json.loads(route.calls[0].request.content)["generationConfig"]["responseSchema"]
    assert "maxItems" not in schema["properties"]["findings"]


@respx.mock
async def test_an_empty_recitation_response_is_retried(provider):
    """RECITATION suppresses the output but not the request.

    Seen on a fixture mid-run; the same case then scored 2/2 on both immediate
    retries, so failing the review on the first one throws away a good answer.
    """
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": []}, "finishReason": "RECITATION"}]},
            ),
            httpx.Response(200, json=ok_body()),
        ]
    )

    result = await complete(provider)

    assert route.call_count == 2
    assert result.data == {"summary": "fine", "findings": []}


@respx.mock
async def test_a_safety_block_is_never_retried(provider):
    """The one empty-text case that is a property of the content, not the draw."""
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        )
    )

    with pytest.raises(LLMError, match="safety"):
        await complete(provider)

    assert route.call_count == 1
