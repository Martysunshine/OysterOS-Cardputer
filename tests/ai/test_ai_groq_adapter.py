"""OpenAI-compatible (Groq) adapter: request shape and error handling via httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest
import truststore
from oyster.ai.openai_compat import OpenAICompatProvider, check_base_url, tls_context
from oyster.ai.provider import ProviderError, ProviderMessage
from oyster.ai.tools import load_lab_profile, offered_tools

KEY = "gsk" "_test_secret_key_0123456789abcdef"
GROQ = "https://api.groq.com/openai/v1"


def make(handler, **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=GROQ, api_key=KEY, model="openai/gpt-oss-20b", transport=httpx.MockTransport(handler), **kw
    )


def ok_response(message: dict, usage=(120, 30), finish="tool_calls") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "openai/gpt-oss-20b",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
        },
    )


CONVERSATION = [
    ProviderMessage(role="system", content="sys"),
    ProviderMessage(role="user", content="Owner question:\nIs my sensor public?"),
    ProviderMessage(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "ble_read_profile_value",
                    "arguments": '{"characteristic_id":"public_sensor","target_id":"target-lab-01"}',
                },
            }
        ],
    ),
    ProviderMessage(
        role="tool", content='{"kind":"job_result","event_ids":["evt-000018"]}', tool_call_id="call_1"
    ),
]


def test_request_shape_matches_groq_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return ok_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps(
                                {
                                    "answer_markdown": "Public value (evt-000018).",
                                    "cited_evidence_ids": ["evt-000018"],
                                }
                            ),
                        },
                    }
                ],
            }
        )

    p = make(handler)
    tools = offered_tools(["ble_read_profile_value"], [], load_lab_profile())
    res = p.propose_action(CONVERSATION, tools)
    body = seen["body"]
    assert seen["method"] == "POST" and seen["url"] == GROQ + "/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["tool_choice"] == "auto"
    assert body["max_completion_tokens"] == 1500
    assert all(
        t["type"] == "function" and {"name", "description", "parameters"} <= set(t["function"])
        for t in body["tools"]
    )
    for forbidden in ("n", "logprobs", "logit_bias", "top_logprobs", "response_format"):
        assert forbidden not in body
    for m in body["messages"]:
        assert "name" not in m  # Groq rejects messages[].name
    assistant, tool = body["messages"][2], body["messages"][3]
    assert assistant["content"] is None and assistant["tool_calls"][0]["type"] == "function"
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
    assert tool == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"kind":"job_result","event_ids":["evt-000018"]}',
    }
    # response normalized into a validated final answer + usage
    assert res.parse_error is None
    assert res.actions[0].kind == "final_answer"
    assert res.actions[0].final.cited_evidence_ids == ["evt-000018"]
    assert (res.usage.input_tokens, res.usage.output_tokens, res.usage.provider_requests) == (120, 30, 1)


def test_output_cap_from_settings_and_explain_results_offers_only_final_answer():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok_response({"role": "assistant", "content": "plain text"}, finish="stop")

    p = make(handler, max_output_tokens=321)
    res = p.explain_results(CONVERSATION)
    assert seen["body"]["max_completion_tokens"] == 321
    assert [t["function"]["name"] for t in seen["body"]["tools"]] == ["final_answer"]
    assert res.actions == [] and res.parse_error == "response contained no tool call"
    assert res.raw_text == "plain text"


def test_tool_calls_parsed_including_malformed_arguments():
    def handler(request):
        return ok_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "ble_inspect_gatt",
                            "arguments": '{"target_id":"target-lab-01"}',
                        },
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "ble_read_profile_value", "arguments": '{"target_id": '},
                    },
                    {
                        "id": "c3",
                        "type": "function",
                        "function": {"name": "wifi_survey", "arguments": "[1,2]"},
                    },
                ],
            }
        )

    res = make(handler).propose_action(CONVERSATION, [])
    assert [a.provider_call_id for a in res.actions] == ["c1", "c2", "c3"]
    assert res.actions[0].arguments == {"target_id": "target-lab-01"}
    assert res.actions[1].arguments is None and res.actions[2].arguments is None
    assert "not valid JSON" in res.parse_error and "JSON object" in res.parse_error
    assert "target_id" not in res.parse_error  # no argument values echoed


def test_tool_use_failed_is_a_parse_error_not_a_transport_failure():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Failed to call a function. Please adjust your prompt.",
                    "type": "invalid_request_error",
                    "code": "tool_use_failed",
                    "failed_generation": "<function=wifi_survey>{bad",
                }
            },
        )

    res = make(handler).propose_action(CONVERSATION, [])
    assert res.actions == [] and "tool_use_failed" in res.parse_error
    assert res.usage.provider_requests == 1


@pytest.mark.parametrize(
    "status,payload,fragment",
    [
        (500, {"error": {"message": "internal", "code": "server_error"}}, "HTTP 500"),
        (503, {"error": {"message": "over capacity"}}, "HTTP 503"),
        (429, {"error": {"message": "rate limit", "code": "rate_limit_exceeded"}}, "rate_limit_exceeded"),
        (401, {"error": {"message": f"Invalid API Key {KEY}", "code": "invalid_api_key"}}, "invalid_api_key"),
        (400, {"error": {"message": "'messages.1' : property 'name' is unsupported"}}, "HTTP 400"),
    ],
)
def test_http_errors_raise_provider_error_without_leaking_key(status, payload, fragment):
    p = make(lambda request: httpx.Response(status, json=payload))
    with pytest.raises(ProviderError) as ei:
        p.propose_action(CONVERSATION, [])
    assert fragment in str(ei.value)
    assert KEY not in str(ei.value)


@pytest.mark.parametrize(
    "exc,fragment",
    [
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.ConnectTimeout("timed out"), "timeout"),
        (httpx.ConnectError("connection refused"), "transport error"),
    ],
)
def test_transport_failures_raise_provider_error(exc, fragment):
    def handler(request):
        raise exc

    with pytest.raises(ProviderError) as ei:
        make(handler, timeout_s=7).propose_action(CONVERSATION, [])
    assert fragment in str(ei.value)


def test_non_json_and_empty_choices_raise_provider_error():
    with pytest.raises(ProviderError):
        make(lambda r: httpx.Response(200, text="<html>proxy</html>")).propose_action(CONVERSATION, [])
    with pytest.raises(ProviderError):
        make(lambda r: httpx.Response(200, json={"choices": []})).propose_action(CONVERSATION, [])


def test_list_models_and_model_required():
    def handler(request):
        assert request.method == "GET" and str(request.url) == GROQ + "/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "openai/gpt-oss-120b", "active": True},
                    {"id": "whisper-large-v3", "active": True},
                ],
            },
        )

    p = make(handler)
    assert [m["id"] for m in p.list_models()] == ["openai/gpt-oss-120b", "whisper-large-v3"]
    unset = OpenAICompatProvider(base_url=GROQ, api_key=KEY, model="", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        unset.propose_action(CONVERSATION, [])


def test_base_url_and_tls_policy():
    check_base_url(GROQ)
    check_base_url("http://127.0.0.1:11434/v1")
    check_base_url("http://localhost:8080/v1")
    with pytest.raises(ValueError):
        check_base_url("http://api.groq.com/openai/v1")
    with pytest.raises(ValueError):
        OpenAICompatProvider(base_url=GROQ, api_key="", model="m")
    assert isinstance(tls_context(), truststore.SSLContext)


def test_wire_messages_never_carry_name_or_unknown_roles():
    from oyster.ai.openai_compat import wire_message

    with pytest.raises(ValueError):
        wire_message(ProviderMessage(role="developer", content="x"))
    with pytest.raises(ValueError):
        wire_message(ProviderMessage(role="tool", content="x"))
    for m in CONVERSATION:
        assert "name" not in wire_message(m)
