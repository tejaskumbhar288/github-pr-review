"""Provider-agnostic LLM interface.

Every backend implements exactly one method. The contract stays narrow so the
review engine never learns which model is behind it, but the return type is a
small envelope rather than a bare dict: token counts and latency are needed to
trace cost per review (roadmap 4), and threading them back through a side
channel would have been stateful and unsafe under concurrency.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

log = logging.getLogger(__name__)

MAX_BACKOFF = 60.0


class LLMError(RuntimeError):
    """Non-retryable provider failure."""


class RateLimited(RuntimeError):
    """Retryable: provider returned 429 or a transient 5xx."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.prompt_tokens,
            "output": self.completion_tokens,
            "total": self.total_tokens,
        }


@dataclass
class LLMResponse:
    """A parsed JSON payload plus the metadata needed to trace the call."""

    data: dict[str, Any]
    model: str = ""
    provider: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    attempts: int = 1


class LLMProvider(abc.ABC):
    name: str = "unknown"
    model: str = ""

    @abc.abstractmethod
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Return the model's response parsed as a JSON object, plus call metadata."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None

    async def __aenter__(self) -> LLMProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


@dataclass
class BackoffResult:
    value: Any
    attempts: int
    elapsed_ms: float


async def with_backoff(fn, *, max_retries: int = 5) -> BackoffResult:
    """Exponential backoff with full jitter around a coroutine factory.

    Free tiers throttle aggressively and change quotas without notice, so this
    is load-bearing rather than defensive decoration. The attempt count is
    returned because a review that succeeded on the fifth try is worth seeing
    in the traces.
    """
    started = time.perf_counter()
    delay = 1.0
    last: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            value = await fn()
        except RateLimited as exc:
            last = exc
            if attempt == max_retries:
                break
            wait = exc.retry_after if exc.retry_after is not None else delay
            wait = min(wait, MAX_BACKOFF)
            # Full jitter: avoids a thundering herd of workers retrying in lockstep.
            wait = random.uniform(0, wait) if wait > 0 else 0
            log.warning(
                "rate limited (attempt %d/%d), sleeping %.1fs: %s",
                attempt,
                max_retries,
                wait,
                exc,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, MAX_BACKOFF)
        else:
            return BackoffResult(
                value=value,
                attempts=attempt,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

    raise LLMError(f"exhausted {max_retries} attempts: {last}") from last


def retry_after_seconds(raw: str | None) -> float | None:
    """Parse a Retry-After header, which may be seconds *or* an HTTP date."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC) if target.tzinfo else _dt.datetime.now()
    return max(0.0, (target - now).total_seconds())


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?|\n?```\s*$")


def parse_json_payload(text: str) -> dict[str, Any]:
    """Parse model output that may be wrapped in markdown fences or prose."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise LLMError("model returned an empty response")

    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()

    try:
        return _as_object(json.loads(cleaned))
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object. A naive find/rfind breaks on
    # trailing prose containing braces, so scan with a depth counter that knows
    # about strings and escapes.
    extracted = _extract_object(cleaned)
    if extracted is None:
        raise LLMError(f"model did not return JSON: {cleaned[:300]!r}")
    try:
        return _as_object(json.loads(extracted))
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned malformed JSON: {extracted[:300]!r}") from exc


def _as_object(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise LLMError(f"expected a JSON object at the top level, got {type(parsed).__name__}")
    return parsed


def _extract_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
