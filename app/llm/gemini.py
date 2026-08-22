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
    TokenUsage,
    parse_json_payload,
    retry_after_seconds,
    with_backoff,
)

BASE = "https://generativelanguage.googleapis.com/v1beta"

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
    ):
        if not api_key:
            raise LLMError("GeminiProvider requires an API key")
        self._key = api_key
        self.model = model
        self._max_retries = max_retries
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
            },
        }

        async def _call() -> dict[str, Any]:
            try:
                resp = await self._client.post(
                    url, json=payload, headers={"x-goog-api-key": self._key}
                )
            except httpx.TimeoutException as exc:
                raise RateLimited(f"gemini timed out: {exc}") from exc
            except httpx.TransportError as exc:
                raise LLMError(f"cannot reach the Gemini API: {exc}") from exc

            if resp.status_code == 429:
                retry = retry_after_seconds(resp.headers.get("retry-after"))
                raise RateLimited("gemini quota exceeded", retry)
            if resp.status_code >= 500:
                raise RateLimited(f"gemini {resp.status_code}")
            if resp.status_code == 400 and "API key" in resp.text:
                raise LLMError("gemini rejected the API key - check GEMINI_API_KEY")
            if resp.status_code >= 400:
                raise LLMError(f"gemini {resp.status_code}: {resp.text[:400]}")
            return resp.json()

        outcome = await with_backoff(_call, max_retries=self._max_retries)
        data = outcome.value
        return LLMResponse(
            data=parse_json_payload(_extract_text(data)),
            model=self.model,
            provider=self.name,
            usage=_extract_usage(data),
            latency_ms=outcome.elapsed_ms,
            attempts=outcome.attempts,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
    if text:
        return text

    finish = candidate.get("finishReason", "unknown")
    if finish == "MAX_TOKENS":
        raise LLMError(
            "gemini hit the output token limit before emitting any JSON - "
            "lower MAX_FILES or CONTEXT_CHAR_LIMIT"
        )
    if finish == "SAFETY":
        raise LLMError("gemini blocked the response on safety grounds")
    raise LLMError(f"gemini returned empty text (finishReason={finish})")
