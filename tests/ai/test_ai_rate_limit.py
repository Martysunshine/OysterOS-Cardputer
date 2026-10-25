"""Provider rate limits (HTTP 429): bounded retry that honours the provider's hint, typed errors, no
identifier leaks. Uses httpx.MockTransport and an injected sleep (no network, no real waiting)."""

from __future__ import annotations

import httpx
import pytest
from oyster.ai.openai_compat import OpenAICompatProvider, ProviderRateLimited, retry_after_seconds
from oyster.ai.provider import ProviderError, ProviderMessage

KEY = "gsk" "_rate_limit_test_key_0123456789"
GROQ = "https://api.groq.com/openai/v1"
MESSAGES = [ProviderMessage(role="system", content="s"), ProviderMessage(role="user", content="q")]


def limited(message: str, headers: dict | None = None) -> httpx.Response:
    body = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
    return httpx.Response(429, json=body, headers=headers or {})


def success() -> httpx.Response:
    msg = {"role": "assistant", "content": "ok"}
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


def provider(responses: list[httpx.Response], sleeps: list[float], **kw) -> tuple[OpenAICompatProvider, list]:
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0)

    p = OpenAICompatProvider(
        base_url=GROQ,
        api_key=KEY,
        model="openai/gpt-oss-120b",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kw,
    )
    return p, calls


@pytest.mark.parametrize(
    ("header", "message", "expected"),
    [
        (None, "Please try again in 10.335s. Need more tokens?", 10.335),
        (None, "Please try again in 7m12.96s.", 432.96),
        (None, "Please try again in 460ms.", 0.46),
        ("3", "Please try again in 10s", 3.0),  # the header wins
        ("not-a-number", "Please try again in 2s", 2.0),
        (None, "no hint here", None),
        (None, "", None),
    ],
)
def test_retry_hint_parsing(header, message, expected):
    got = retry_after_seconds(header, message)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_rate_limit_is_waited_out_once_then_succeeds():
    sleeps: list[float] = []
    p, calls = provider([limited("Please try again in 2.5s."), success()], sleeps)
    result = p.explain_results(MESSAGES)
    assert result.raw_text == "ok"
    assert len(calls) == 2
    assert sleeps == [pytest.approx(3.0)]  # hint plus a small margin


def test_retry_after_header_is_preferred():
    sleeps: list[float] = []
    p, _ = provider([limited("Please try again in 15s", {"retry-after": "4"}), success()], sleeps)
    p.explain_results(MESSAGES)
    assert sleeps == [pytest.approx(4.5)]


def test_retries_are_bounded_and_the_error_is_typed():
    sleeps: list[float] = []
    p, calls = provider([limited("Please try again in 1s")] * 3, sleeps, max_rate_limit_retries=2)
    with pytest.raises(ProviderRateLimited) as exc:
        p.explain_results(MESSAGES)
    assert len(calls) == 3 and len(sleeps) == 2
    assert exc.value.retry_after_s == pytest.approx(1.0)
    assert "rate limit" in str(exc.value) and isinstance(exc.value, ProviderError)


def test_a_long_wait_is_not_slept_through():
    sleeps: list[float] = []
    p, calls = provider([limited("Please try again in 45s")], sleeps, max_retry_wait_s=20)
    with pytest.raises(ProviderRateLimited) as exc:
        p.explain_results(MESSAGES)
    assert sleeps == [] and len(calls) == 1
    assert exc.value.retry_after_s == pytest.approx(45.0)
    assert "about 45 s" in str(exc.value)


def test_no_hint_means_no_retry():
    sleeps: list[float] = []
    p, calls = provider([limited("slow down")], sleeps)
    with pytest.raises(ProviderRateLimited):
        p.explain_results(MESSAGES)
    assert sleeps == [] and len(calls) == 1


def test_retries_can_be_disabled():
    sleeps: list[float] = []
    p, calls = provider([limited("Please try again in 1s")], sleeps, max_rate_limit_retries=0)
    with pytest.raises(ProviderRateLimited):
        p.explain_results(MESSAGES)
    assert sleeps == [] and len(calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_not_retried(status):
    sleeps: list[float] = []
    p, calls = provider([httpx.Response(status, json={"error": {"message": "boom", "code": "x"}})], sleeps)
    with pytest.raises(ProviderError) as exc:
        p.explain_results(MESSAGES)
    assert not isinstance(exc.value, ProviderRateLimited)
    assert sleeps == [] and len(calls) == 1


def test_organization_ids_and_keys_never_appear_in_the_error():
    org = "org_" + "01abcdefghij0123456789"
    text = f"Rate limit reached in organization `{org}` for key {KEY}. Please try again in 90s."
    p, _ = provider([limited(text)], [])
    with pytest.raises(ProviderRateLimited) as exc:
        p.explain_results(MESSAGES)
    shown = str(exc.value)
    assert org not in shown and KEY not in shown
    assert "org_<redacted>" in shown
