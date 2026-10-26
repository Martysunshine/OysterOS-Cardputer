"""A provider that refuses a request as too large (HTTP 413).

Seen on the device with a free-tier model: the second request, carrying a Wi-Fi survey and a BLE discovery,
exceeded the provider's per-minute input cap. The orchestrator calibrates its token estimate from the
provider's message, shrinks the context and retries once; a second refusal ends the run as ai_unavailable."""

from __future__ import annotations

from oyster.ai import fake as F
from oyster.ai.openai_compat import ProviderRequestTooLarge
from test_ai_orchestrator import READ_PUBLIC, cite_last, make_env

TOO_LARGE = (
    "provider refused the request as too large (HTTP 413) rate_limit_exceeded: Request too large for "
    "model `m` on input tokens per minute (ITPM): Limit 7000, Requested 7990, please reduce your message size"
)


class RefusesOnce:
    """Wraps the scripted provider: the Nth propose_action call raises 413, the rest pass through."""

    def __init__(self, inner, refuse_calls: set[int]):
        self.inner = inner
        self.refuse_calls = refuse_calls
        self.n = 0

    def propose_action(self, messages, tools):
        self.n += 1
        if self.n in self.refuse_calls:
            raise ProviderRequestTooLarge(TOO_LARGE)
        return self.inner.propose_action(messages, tools)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_413_is_retried_once_with_a_calibrated_estimate(tmp_path):
    env = make_env(tmp_path, [F.tool_call("ble_read_profile_value", READ_PUBLIC), cite_last])
    env.orch.provider = RefusesOnce(env.orch.provider, {2})
    run = env.execute()
    assert run.status == "succeeded", run.error
    retried = [s for s in run.steps if "too large" in s["summary"]]
    assert len(retried) == 1 and "factor" in retried[0]["summary"]
    assert run.provider_requests_used == 2  # the refused attempt is not counted as a model request


def test_a_second_413_ends_the_run(tmp_path):
    env = make_env(tmp_path, [F.tool_call("ble_read_profile_value", READ_PUBLIC), cite_last])
    env.orch.provider = RefusesOnce(env.orch.provider, {2, 3})
    run = env.execute()
    assert run.status == "ai_unavailable"
    assert "too large" in (run.error or "")
