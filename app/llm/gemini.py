"""Gemini backend, talking to the REST generateContent endpoint directly.

Deliberately no vendor SDK: one less dependency to break, and the wire format
has been stable far longer than the Python clients have.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    RateLimited,
    SamplingFailure,
    TokenUsage,
    parse_json_payload,
    retry_after_seconds,
    with_backoff,
)

BASE = "https://generativelanguage.googleapis.com/v1beta"

# Set explicitly rather than inherited, because the per-model default varies
# and a review that finishes on one model should not be truncated on another.
#
# The size is deliberate. Reasoning is charged to this same budget, and on real
# pull requests it dominates: measured spends of thinking=6745/answer=1432 and
# thinking=7860/answer=317 against an 8192 cap, where the answer had plenty of
# room and the thinking had none. Sizing this to the length of a review is
# therefore the wrong instinct - it has to cover the model's reasoning about a
# diff it has never seen, which scales with the diff, not with the verdict.
MAX_OUTPUT_TOKENS = 32768

# Response schema enforced server-side. Native structured output is far more
# reliable than describing the shape in the prompt and hoping.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "category": {
                        "type": "string",
                        "enum": [
                            "correctness",
                            "security",
                            "performance",
                            "error-handling",
                            "maintainability",
                        ],
                    },
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["file", "line", "severity", "category", "title", "detail"],
            },
        },
    },
    "required": ["summary", "findings"],
}


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 5,
        timeout: float = 180.0,
        client: httpx.AsyncClient | None = None,
        max_sampling_retries: int = 2,
    ):
        if not api_key:
            raise LLMError("GeminiProvider requires an API key")
        self._key = api_key
        self.model = model
        self._max_retries = max_retries
        self._max_sampling_retries = max_sampling_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def complete_json(
        self, *, system: str, user: str, temperature: float = 0.2
    ) -> LLMResponse:
        url = f"{BASE}/models/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        # How many times the model has failed to produce a usable answer. Not a
        # counter for its own sake: it is what makes each re-roll different from
        # the attempt that failed. See _sampling_temperature.
        rerolls = 0

        async def _call() -> tuple[dict[str, Any], str]:
            nonlocal rerolls
            payload["generationConfig"]["temperature"] = _sampling_temperature(temperature, rerolls)
            try:
                resp = await self._client.post(
                    url, json=payload, headers={"x-goog-api-key": self._key}
                )
            except httpx.TimeoutException as exc:
                raise RateLimited(f"gemini timed out: {exc}") from exc
            except httpx.TransportError as exc:
                raise LLMError(f"cannot reach the Gemini API: {exc}") from exc

            if resp.status_code == 429:
                raise _quota_error(resp)
            if resp.status_code >= 500:
                raise RateLimited(f"gemini {resp.status_code}")
            if resp.status_code == 400 and "API key" in resp.text:
                raise LLMError("gemini rejected the API key - check GEMINI_API_KEY")
            if resp.status_code >= 400:
                raise LLMError(f"gemini {resp.status_code}: {resp.text[:400]}")

            # Extract inside the retried call, not after it. A truncated
            # response is a sampling accident rather than a property of the
            # request - the same prompt that ran to 26k answer tokens and was
            # cut off produced a complete 643-token review on the next attempt -
            # so it has to be raised where the backoff can still see it.
            data = resp.json()
            try:
                return data, _extract_text(data)
            except SamplingFailure:
                rerolls += 1
                raise

        outcome = await with_backoff(
            _call,
            max_retries=self._max_retries,
            max_sampling_retries=self._max_sampling_retries,
        )
        data, text = outcome.value
        return LLMResponse(
            data=parse_json_payload(text),
            model=self.model,
            provider=self.name,
            usage=_extract_usage(data),
            latency_ms=outcome.elapsed_ms,
            attempts=outcome.attempts,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# How far each re-roll nudges the temperature, and the ceiling it stops at.
# 0.35 is enough to leave the neighbourhood of a stuck decode; 0.9 keeps the
# output recognisably a code review rather than a creative writing exercise.
REROLL_TEMPERATURE_STEP = 0.35
MAX_SAMPLING_TEMPERATURE = 0.9


def _sampling_temperature(base: float, rerolls: int) -> float:
    """Raise the temperature on each re-roll after a sampling failure.

    Retrying is the right response to a truncated or empty response - the same
    prompt that stopped at answer=26383 returned a complete 643-token review
    moments later. But reviews run at temperature 0.2, where decoding is close
    to deterministic, so *re-sending the identical request is the least likely
    thing to change the outcome*. A fixture whose whole file is 400 characters
    burned two attempts producing answer=30893 and then answer=31638 tokens:
    that is a decode stuck in a loop, and it stayed stuck because nothing about
    the second attempt differed from the first.

    Nudging the temperature is what makes a retry a genuinely new sample. It
    costs nothing, applies only after a failure, and leaves the first attempt -
    the one that succeeds almost always - at the low temperature a review wants.
    """
    if rerolls <= 0:
        return base
    return min(base + REROLL_TEMPERATURE_STEP * rerolls, MAX_SAMPLING_TEMPERATURE)


def _quota_error(resp: httpx.Response) -> Exception:
    """Classify a 429: a daily cap is terminal, everything else is transient.

    Both arrive as RESOURCE_EXHAUSTED, but retrying a per-day quota does not
    just fail - each attempt spends another request from the exhausted budget,
    so five retries across four eval cases can burn a whole day's free tier
    without a single call succeeding. Google makes this easy to get wrong: the
    body carries a RetryInfo of ~40s even when the quota does not reset for
    hours.
    """
    body = _error_body(resp)
    for violation in _quota_violations(body):
        quota_id = str(violation.get("quotaId") or "")
        if "PerDay" not in quota_id:
            continue
        model = (violation.get("quotaDimensions") or {}).get("model") or "the model"
        limit = violation.get("quotaValue")
        cap = f" ({limit}/day" if limit else " ("
        return LLMError(
            f"gemini daily quota exhausted for {model}{cap} on the free tier); "
            "it resets at midnight Pacific, and retrying spends requests you "
            "no longer have - switch models, use LLM_PROVIDER=ollama, or wait"
        )

    retry = retry_after_seconds(resp.headers.get("retry-after"))
    if retry is None:
        retry = _retry_info_seconds(body)
    return RateLimited("gemini quota exceeded", retry)


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        parsed = resp.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _quota_violations(body: dict[str, Any]) -> list[dict[str, Any]]:
    details = (body.get("error") or {}).get("details") or []
    out: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("QuotaFailure"):
            continue
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict):
                out.append(violation)
    return out


def _retry_info_seconds(body: dict[str, Any]) -> float | None:
    """Read RetryInfo.retryDelay, a protobuf duration string like "40s"."""
    for detail in (body.get("error") or {}).get("details") or []:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("RetryInfo"):
            continue
        raw = str(detail.get("retryDelay") or "").strip()
        if raw.endswith("s"):
            raw = raw[:-1]
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    return None


def _extract_usage(data: dict[str, Any]) -> TokenUsage:
    meta = data.get("usageMetadata") or {}
    return TokenUsage(
        prompt_tokens=int(meta.get("promptTokenCount") or 0),
        completion_tokens=int(meta.get("candidatesTokenCount") or 0),
    )


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"gemini returned nothing ({reason})")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    finish = candidate.get("finishReason", "unknown")

    # Check why the model stopped *before* trusting the text. A truncated
    # response usually still carries most of its JSON, so returning it here
    # sent half an object to the parser, which could only report "model did
    # not return JSON" - hiding the one cause the caller can actually act on.
    if finish == "MAX_TOKENS":
        # Retryable, on the sampling budget rather than the rate-limit one: the
        # request was already billed, so a re-roll is not free. If every attempt
        # truncates, the prompt really is too big and the message says which
        # knobs to turn.
        raise SamplingFailure(
            f"gemini hit the {MAX_OUTPUT_TOKENS}-token output limit mid-response"
            f"{_token_breakdown(data)} - retrying; if this persists, "
            "lower MAX_FILES or CONTEXT_CHAR_LIMIT"
        )
    # Terminal: a safety block is a decision about the content, so the next
    # attempt reaches the same decision.
    if finish == "SAFETY":
        raise LLMError("gemini blocked the response on safety grounds")

    if text:
        return text

    # Everything else that yields no text is the model failing to answer this
    # time rather than a property of the request, so it is worth another go.
    # RECITATION is the one seen in practice - Gemini suppresses output it
    # believes reproduces training data - and it is plainly intermittent: the
    # fixture that tripped it scored 2/2 on both immediate retries.
    raise SamplingFailure(f"gemini returned empty text (finishReason={finish})")


def _token_breakdown(data: dict[str, Any]) -> str:
    """Say where the output budget went.

    Worth the few lines: on models that think before answering, the reasoning
    is charged to the same budget, so "thinking=8100, answer=92" and
    "answer=8192" call for completely different fixes.
    """
    meta = data.get("usageMetadata") or {}
    spent = ", ".join(
        f"{label}={meta[key]}"
        for key, label in (
            ("thoughtsTokenCount", "thinking"),
            ("candidatesTokenCount", "answer"),
            ("promptTokenCount", "prompt"),
        )
        if meta.get(key)
    )
    return f" ({spent})" if spent else ""
