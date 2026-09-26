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
# A re-roll waits only long enough to avoid hammering; see with_backoff.
SAMPLING_RETRY_DELAY = 1.0


class LLMError(RuntimeError):
    """Non-retryable provider failure."""


class RateLimited(RuntimeError):
    """Retryable: provider returned 429 or a transient 5xx."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SamplingFailure(RateLimited):
    """Retryable, but for a different reason and on a different budget.

    The request was accepted and billed; the model just did not produce a usable
    answer this time - it truncated, returned an empty candidate, or blocked
    itself on recitation. Retrying is right, because the failure is sampling
    variance rather than a property of the prompt: the request that stopped at
    ``answer=26383`` returned a complete 643-token review moments later.

    It is a subclass so every existing ``except RateLimited`` still catches it,
    but ``with_backoff`` gives it its own, smaller allowance. A rate limit is the
    provider asking us to wait, and waiting costs nothing. A re-roll spends
    another request from a cap that may be 20 for the whole day.
    """


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


async def with_backoff(fn, *, max_retries: int = 5, max_sampling_retries: int = 3) -> BackoffResult:
    """Exponential backoff with full jitter around a coroutine factory.

    Free tiers throttle aggressively and change quotas without notice, so this
    is load-bearing rather than defensive decoration. The attempt count is
    returned because a review that succeeded on the fifth try is worth seeing
    in the traces.

    The two retryable failures are counted separately, because they cost
    differently:

    * A ``RateLimited`` is the provider telling us to wait. Waiting is free, so
      it gets the full ``max_retries`` budget and an exponentially growing sleep.
    * A ``SamplingFailure`` is the model failing to answer a request we already
      paid for. Retrying spends another request from a per-day cap that may be
      20, so it gets ``max_sampling_retries`` and retries almost immediately -
      there is nothing to wait *for*, and the point of the delay was politeness
      to a rate limiter that is not involved.

    Exhausting either budget ends the call. Keeping one shared counter meant a
    single unlucky draw could consume five requests instead of two.
    """
    started = time.perf_counter()
    delay = 1.0
    last: Exception | None = None
    attempt = 0
    rate_limit_retries = 0
    sampling_retries = 0

    while True:
        attempt += 1
        try:
            value = await fn()
        except SamplingFailure as exc:
            last = exc
            sampling_retries += 1
            if sampling_retries >= max_sampling_retries:
                break
            # No exponential growth: the provider is not asking us to slow down,
            # and a longer sleep just makes a re-roll take longer.
            wait = random.uniform(0, SAMPLING_RETRY_DELAY)
            log.warning(
                "model did not answer (sampling retry %d/%d), re-rolling in %.1fs: %s",
                sampling_retries,
                max_sampling_retries,
                wait,
                exc,
            )
            await asyncio.sleep(wait)
        except RateLimited as exc:
            last = exc
            rate_limit_retries += 1
            if rate_limit_retries >= max_retries:
                break
            wait = exc.retry_after if exc.retry_after is not None else delay
            wait = min(wait, MAX_BACKOFF)
            # Full jitter: avoids a thundering herd of workers retrying in lockstep.
            wait = random.uniform(0, wait) if wait > 0 else 0
            log.warning(
                "rate limited (attempt %d/%d), sleeping %.1fs: %s",
                rate_limit_retries,
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

    # Say which budget ran out. "exhausted 5 attempts" on a run that made two
    # requests and hit the sampling cap is the kind of report that sends you
    # looking for a rate limit that never happened.
    if isinstance(last, SamplingFailure):
        raise LLMError(
            f"the model failed to return a usable answer {sampling_retries} time(s) "
            f"in a row (MAX_SAMPLING_RETRIES={max_sampling_retries}): {last}"
        ) from last
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
