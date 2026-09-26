"""Ollama backend for fully local, zero-egress review.

Slower and weaker than a hosted frontier model, but this is the path for any
codebase that legally cannot leave the building.
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
    with_backoff,
)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        host: str,
        model: str,
        max_retries: int = 3,
        num_ctx: int = 16384,
        timeout: float = 600.0,
        client: httpx.AsyncClient | None = None,
        max_sampling_retries: int = 2,
    ):
        self._host = host.rstrip("/")
        self.model = model
        self._max_retries = max_retries
        self._max_sampling_retries = max_sampling_retries
        self._num_ctx = num_ctx
        self._owns_client = client is None
        # Local generation on modest hardware is slow; be patient.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def complete_json(
        self, *, system: str, user: str, temperature: float = 0.2
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_ctx": self._num_ctx},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        async def _call() -> tuple[dict[str, Any], str]:
            try:
                resp = await self._client.post(f"{self._host}/api/chat", json=payload)
            except httpx.ConnectError as exc:
                raise LLMError(
                    f"cannot reach ollama at {self._host} - is `ollama serve` running?"
                ) from exc
            except httpx.TimeoutException as exc:
                raise LLMError(
                    f"ollama timed out. A 7B model on limited VRAM can take minutes; "
                    f"raise REQUEST_TIMEOUT or use a smaller model. ({exc})"
                ) from exc

            if resp.status_code == 404:
                raise LLMError(f"ollama has no model {self.model!r}. Run: ollama pull {self.model}")
            if resp.status_code >= 500:
                raise RateLimited(f"ollama {resp.status_code}")
            if resp.status_code >= 400:
                raise LLMError(f"ollama {resp.status_code}: {resp.text[:400]}")

            # Extract inside the retried call, not after it - the same shape of
            # bug the Gemini path had. A local model that returns nothing has
            # failed to sample, not failed the request, and the next draw is
            # usually fine. Checking it out here made an empty message fail the
            # whole review with no retry possible.
            data = resp.json()
            content = (data.get("message") or {}).get("content", "")
            if not content:
                raise SamplingFailure("ollama returned an empty message")
            return data, content

        outcome = await with_backoff(
            _call,
            max_retries=self._max_retries,
            max_sampling_retries=self._max_sampling_retries,
        )
        data, content = outcome.value

        return LLMResponse(
            data=parse_json_payload(content),
            model=self.model,
            provider=self.name,
            usage=TokenUsage(
                prompt_tokens=int(data.get("prompt_eval_count") or 0),
                completion_tokens=int(data.get("eval_count") or 0),
            ),
            latency_ms=outcome.elapsed_ms,
            attempts=outcome.attempts,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
