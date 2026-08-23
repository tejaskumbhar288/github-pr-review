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
from app.llm.gemini import BASE, GeminiProvider

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
