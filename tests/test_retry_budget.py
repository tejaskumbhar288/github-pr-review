"""Separate retry budgets for rate limits and sampling failures (note 6).

Making truncation and empty responses retryable was right, but it put them on
the rate-limit budget, so one unlucky draw could cost five requests against a
20/day cap. These pin the split: waiting is cheap and gets the big budget,
re-rolling is expensive and gets the small one.
"""

from __future__ import annotations

import pytest

from app.llm.base import LLMError, RateLimited, SamplingFailure, with_backoff

pytestmark = pytest.mark.usefixtures("no_sleep")


def _always(exc_factory):
    calls = []

    async def fn():
        calls.append(1)
        raise exc_factory()

    return fn, calls


async def test_a_sampling_failure_spends_only_the_sampling_budget():
    fn, calls = _always(lambda: SamplingFailure("truncated"))
    with pytest.raises(LLMError) as err:
        await with_backoff(fn, max_retries=5, max_sampling_retries=2)

    assert len(calls) == 2, "a re-roll must not spend the rate-limit budget"
    assert "MAX_SAMPLING_RETRIES=2" in str(err.value)


async def test_a_rate_limit_still_gets_the_full_budget():
    fn, calls = _always(lambda: RateLimited("429", retry_after=0))
    with pytest.raises(LLMError, match="exhausted 5 attempts"):
        await with_backoff(fn, max_retries=5, max_sampling_retries=2)
    assert len(calls) == 5


async def test_the_two_budgets_are_counted_separately():
    """A run that is rate limited *and* unlucky gets both allowances, not one.

    The shared counter meant three 429s left only two attempts for the model to
    actually answer in - so a review could fail having never once been sampled
    successfully, and the error blamed the rate limit.
    """
    seen = []

    async def fn():
        seen.append(len(seen))
        if len(seen) <= 3:
            raise RateLimited("429", retry_after=0)
        if len(seen) == 4:
            raise SamplingFailure("empty candidate")
        return "review"

    out = await with_backoff(fn, max_retries=5, max_sampling_retries=2)
    assert out.value == "review"
    assert out.attempts == 5


async def test_the_error_says_which_budget_ran_out():
    """ "exhausted 5 attempts" on a run that made two sends the reader hunting
    for a rate limit that never happened."""
    fn, _ = _always(lambda: SamplingFailure("recitation"))
    with pytest.raises(LLMError) as err:
        await with_backoff(fn, max_retries=5, max_sampling_retries=2)
    message = str(err.value)
    assert "failed to return a usable answer" in message
    assert "exhausted 5 attempts" not in message


async def test_a_sampling_failure_is_still_a_rate_limited_for_existing_handlers():
    assert issubclass(SamplingFailure, RateLimited)


async def test_success_on_the_first_try_costs_nothing():
    async def fn():
        return 42

    out = await with_backoff(fn, max_retries=5, max_sampling_retries=2)
    assert out.value == 42 and out.attempts == 1


# --- making the re-roll actually different ---------------------------------


def test_the_first_attempt_keeps_the_low_review_temperature():
    from app.llm.gemini import _sampling_temperature

    assert _sampling_temperature(0.2, 0) == 0.2


def test_each_re_roll_raises_the_temperature():
    """Reviews run at 0.2, where decoding is near-deterministic. Re-sending the
    identical request is the least likely thing to change the outcome - a
    400-character fixture burned two attempts emitting 30,893 then 31,638 answer
    tokens, stuck in the same loop both times."""
    from app.llm.gemini import _sampling_temperature

    first = _sampling_temperature(0.2, 1)
    second = _sampling_temperature(0.2, 2)
    assert 0.2 < first < second


def test_the_temperature_is_capped():
    """Still a code review, not a creative writing exercise."""
    from app.llm.gemini import MAX_SAMPLING_TEMPERATURE, _sampling_temperature

    assert _sampling_temperature(0.2, 99) == MAX_SAMPLING_TEMPERATURE
