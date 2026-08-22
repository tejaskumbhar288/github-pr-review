from __future__ import annotations

import pytest

from app.llm.base import (
    LLMError,
    RateLimited,
    TokenUsage,
    parse_json_payload,
    retry_after_seconds,
    with_backoff,
)


def test_plain_json():
    assert parse_json_payload('{"a": 1}') == {"a": 1}


def test_markdown_fences_are_stripped():
    assert parse_json_payload('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_payload('```\n{"a": 1}\n```') == {"a": 1}


def test_object_is_recovered_from_surrounding_prose():
    text = 'Sure! Here is the review:\n{"summary": "x", "findings": []}\nHope that helps.'
    assert parse_json_payload(text)["summary"] == "x"


def test_trailing_brace_in_prose_does_not_break_extraction():
    """A naive find/rfind would swallow the trailing text and fail to parse."""
    text = '{"a": {"b": 1}} note: use {} for empty'
    assert parse_json_payload(text) == {"a": {"b": 1}}


def test_braces_inside_strings_are_not_counted():
    assert parse_json_payload('{"code": "if (x) { y(); }"}')["code"] == "if (x) { y(); }"


@pytest.mark.parametrize("bad", ["", "   ", "no json here", "[1, 2, 3]", "null"])
def test_non_objects_raise(bad):
    with pytest.raises(LLMError):
        parse_json_payload(bad)


def test_retry_after_accepts_seconds_and_http_dates():
    assert retry_after_seconds("30") == 30.0
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("garbage") is None
    assert retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT") > 0


def test_token_usage_totals():
    u = TokenUsage(prompt_tokens=10, completion_tokens=5)
    assert u.total_tokens == 15
    assert u.as_dict() == {"input": 10, "output": 5, "total": 15}


async def test_backoff_returns_on_first_success():
    calls = 0

    async def ok():
        nonlocal calls
        calls += 1
        return "done"

    out = await with_backoff(ok, max_retries=3)
    assert out.value == "done" and out.attempts == 1 and calls == 1


async def test_backoff_retries_then_succeeds(no_sleep):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RateLimited("429")
        return "done"

    out = await with_backoff(flaky, max_retries=5)
    assert out.value == "done" and out.attempts == 3


async def test_backoff_gives_up_and_says_so(no_sleep):

    async def always():
        raise RateLimited("429")

    with pytest.raises(LLMError, match="exhausted 3 attempts"):
        await with_backoff(always, max_retries=3)


async def test_non_retryable_errors_propagate_immediately():
    calls = 0

    async def broken():
        nonlocal calls
        calls += 1
        raise LLMError("bad key")

    with pytest.raises(LLMError, match="bad key"):
        await with_backoff(broken, max_retries=5)
    assert calls == 1
