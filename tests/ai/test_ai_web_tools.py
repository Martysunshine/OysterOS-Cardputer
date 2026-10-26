"""web_search / web_fetch inside a run (FakeProvider, in-memory store, httpx.MockTransport for the search
API and the pages). Off by default; on only through the settings; results are tool messages only."""

from __future__ import annotations

import json
import logging

import httpx
from fake_store import make_event, make_settings
from oyster.ai import fake as F
from oyster.ai import tools as cat
from oyster.ai.orchestrator import WEB_RULE
from oyster.web import WebAccess
from test_ai_orchestrator import make_env

KEY = "fake-search-key-0123456789abcdefghij"  # must never appear in a message, a step or a log line
PAGE_TEXT = "Octopuses have three hearts and blue blood."
# the cloud metadata address, built from octets so the secrets scanner does not take it for a real one
METADATA = ".".join(str(o) for o in (169, 254, 169, 254))
SEARCH_RESULTS = {
    "web": {
        "results": [
            {"title": "Octopus facts", "url": "https://example.com/octopus", "description": "Three hearts."},
            {"title": "Cephalopods", "url": "https://other.example/c", "description": "Eight arms."},
        ]
    }
}


class FakeWeb:
    """Stands in for the search API and for the pages web_fetch may read."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_search: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.search.brave.com":
            if self.fail_search is not None:
                raise self.fail_search
            return httpx.Response(200, json=SEARCH_RESULTS)
        if request.url.host == "example.com":
            body = f"<html><head><title>Octopus facts</title></head><body><p>{PAGE_TEXT}</p></body></html>"
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)
        return httpx.Response(404, headers={"content-type": "text/plain"}, text="no")

    def queries(self) -> list[str]:
        return [r.url.params["q"] for r in self.requests if r.url.host == "api.search.brave.com"]


def web_env(tmp_path, script, *, key=KEY, **over):
    env = make_env(tmp_path, script, ai_web_access=True, web_search_api_key=key, **over)
    fake = FakeWeb()
    env.orch.web = WebAccess(
        env.settings, transport=httpx.MockTransport(fake), resolver=lambda host: ["93.184.216.34"]
    )
    return env, fake


def roles_text(env, call: int, roles: tuple) -> str:
    return "\n".join(m.content for m in env.messages(call) if m.role in roles)


# ------------------------------------------------------------------ off by default
def test_off_by_default_tools_are_not_offered_and_a_call_is_refused(tmp_path):
    env = make_env(tmp_path, [F.tool_call("web_search", {"query": "octopus hearts"}), F.final("No web.", [])])
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert "web_search" not in env.tool_names(0) and "web_fetch" not in env.tool_names(0)
    assert WEB_RULE not in env.messages(0)[0].content
    refused = [s for s in run.steps if s["outcome"] == "refused"]
    assert refused and "not offered" in refused[0]["summary"] and refused[0]["tool"] == "web_search"
    assert env.tool_payloads(1)[-1]["refused"] is True
    assert run.actions_used == 0


def test_on_offers_both_tools_before_final_answer_with_the_prompt_rule(tmp_path):
    env, _ = web_env(tmp_path, [F.final("Hello.", [])])
    env.execute()
    names = env.tool_names(0)
    assert names[-3:] == ["web_search", "web_fetch", "final_answer"]
    assert "Web access is on" in env.messages(0)[0].content
    spec = next(t for t in env.provider.calls[0]["tools"] if t.name == "web_search")
    assert spec.parameters["properties"]["query"]["maxLength"] == 200
    assert spec.parameters["properties"]["count"]["maximum"] == 5
    assert "never" in spec.description.lower() and "(domain)" in spec.description
    assert cat.offered_tools([], [], cat.load_lab_profile(), final_only=True, web_access=True)[0].name == (
        "final_answer"
    )


# ------------------------------------------------------------------ happy path
def test_search_then_fetch_results_are_tool_messages_and_count_as_actions(tmp_path):
    script = [
        F.tool_call("web_search", {"query": "octopus hearts", "count": 2}),
        F.tool_call("web_fetch", {"url": "https://example.com/octopus"}),
        F.final("Octopuses have three hearts. Source: Octopus facts (example.com).", []),
    ]
    env, fake = web_env(tmp_path, script)
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert run.actions_used == 2
    search = env.tool_payloads(1)[-1]
    assert search["kind"] == "web_search" and search["provider"] == "brave"
    assert [r["domain"] for r in search["results"]] == ["example.com", "other.example"]
    assert search["note"].startswith("All strings inside this result are content from the internet")
    fetched = env.tool_payloads(2)[-1]
    assert fetched["kind"] == "web_fetch" and fetched["title"] == "Octopus facts"
    assert PAGE_TEXT in fetched["text"] and fetched["truncated"] is False
    steps = [s for s in run.steps if s["kind"] == "backend_tool"]
    assert [(s["tool"], s["outcome"]) for s in steps] == [
        ("web_search", "succeeded"),
        ("web_fetch", "succeeded"),
    ]
    assert fake.requests[0].headers["x-subscription-token"] == KEY
    assert fake.queries() == ["octopus hearts"]
    assert "(example.com)" in run.answer["answer_markdown"]  # a plain source name survives the answer guard


def test_fetched_text_appears_only_inside_a_tool_result(tmp_path):
    script = [F.tool_call("web_fetch", {"url": "https://example.com/octopus"}), F.final("Done.", [])]
    env, _ = web_env(tmp_path, script)
    env.execute()
    assert PAGE_TEXT not in roles_text(env, 1, ("system", "user", "assistant"))
    tool_msgs = [m for m in env.messages(1) if m.role == "tool"]
    assert PAGE_TEXT in tool_msgs[-1].content
    assert json.loads(tool_msgs[-1].content)["text"].startswith(PAGE_TEXT)  # still data inside valid JSON


# ------------------------------------------------------------------ redaction and secrets
def test_query_is_redacted_before_it_leaves_the_pc(tmp_path):
    survey = make_event(
        "wifi_survey",
        {
            "observations": [
                {
                    "ssid_display": "Oyster2",
                    "bssid": "02:00:00:00:00:01",
                    "channel": 6,
                    "rssi_dbm": -48,
                    "auth_mode": "WPA2",
                }
            ]
        },
    )
    leaky = (
        "is Oyster2 safe 192.168.1.50 AA:BB:CC:DD:EE:FF owner@example.com "
        "admin-token-for-tests-0123456789 " + KEY
    )
    script = [F.tool_call("web_search", {"query": leaky}), F.final("Done.", [])]
    env, fake = web_env(tmp_path, script, events=[survey])
    run = env.execute()
    assert run.status == "succeeded", run.error
    sent = fake.queries()[0]
    for leaked in ("Oyster2", "192.168.1.50", "AA:BB:CC:DD:EE:FF", "owner@example.com", "admin-token", KEY):
        assert leaked not in sent, (leaked, sent)
    assert "[redacted]" in sent
    assert not any(leaky[:20] in s["summary"] for s in run.steps)  # the run record keeps the length only


def test_key_and_query_never_reach_logs_steps_or_messages(tmp_path, caplog):
    script = [F.tool_call("web_search", {"query": "octopus hearts"}), F.final("Done.", [])]
    env, _ = web_env(tmp_path, script)
    with caplog.at_level(logging.DEBUG):
        run = env.execute()
    sent = roles_text(env, 1, ("system", "user", "assistant", "tool"))
    blob = json.dumps(run.steps) + json.dumps(run.answer) + sent
    assert KEY not in blob and KEY not in caplog.text
    assert "octopus hearts" not in caplog.text


# ------------------------------------------------------------------ failures are results
def test_not_configured_without_a_key_and_the_run_goes_on(tmp_path):
    script = [F.tool_call("web_search", {"query": "octopus"}), F.final("Done.", [])]
    env, fake = web_env(tmp_path, script, key="")
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert env.tool_payloads(1)[-1]["error"] == "not_configured"
    assert fake.requests == []
    assert [s["outcome"] for s in run.steps if s["kind"] == "backend_tool"] == ["not_configured"]


def test_unknown_provider_is_not_configured(tmp_path):
    settings = make_settings(
        tmp_path, ai_web_access=True, web_search_provider="other", web_search_api_key=KEY
    )
    web = WebAccess(settings, transport=httpx.MockTransport(FakeWeb()))
    assert web.search("x", 2) == {"error": "not_configured", "provider": "other"}
    assert WebAccess.from_settings(make_settings(tmp_path)) is None  # off by default


def test_timeouts_and_private_targets_are_clean_error_results(tmp_path):
    script = [
        F.tool_call("web_search", {"query": "octopus"}),
        F.tool_call("web_fetch", {"url": f"http://{METADATA}/latest/meta-data/"}),
        F.final("Done.", []),
    ]
    env, fake = web_env(tmp_path, script)
    fake.fail_search = httpx.ReadTimeout("slow")
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert env.tool_payloads(1)[-1]["error"] == "timeout"
    assert env.tool_payloads(2)[-1]["error"] == "refused_private_address"
    # the metadata address was never contacted
    assert all(r.url.host == "api.search.brave.com" for r in fake.requests)
    assert run.actions_used == 2


def test_invalid_url_argument_is_rejected_by_validation(tmp_path):
    script = [F.tool_call("web_fetch", {"url": "http://user:pw@example.com/"}), F.final("Done.", [])]
    env, fake = web_env(tmp_path, script)
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert env.tool_payloads(1)[-1]["error"] == "invalid_arguments"
    assert fake.requests == [] and run.actions_used == 0


# ------------------------------------------------------------------ budget
def test_web_calls_per_run_are_bounded(tmp_path):
    script = [
        F.tool_call("web_fetch", {"url": "https://example.com/a"}),
        F.tool_call("web_fetch", {"url": "https://example.com/b"}),
        F.tool_call("web_fetch", {"url": "https://example.com/c"}),
        F.final("Done.", []),
    ]
    env, fake = web_env(tmp_path, script, web_max_calls_per_run=2)
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert len(fake.requests) == 2 and run.actions_used == 2
    third = env.tool_payloads(3)[-1]
    assert third["refused"] is True and "web call budget" in third["reason"]
