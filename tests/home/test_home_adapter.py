"""Home Assistant client: success paths, error mapping, and the rule that no secret is ever copied into an
error. The fake instance is an httpx.MockTransport handler; nothing here touches a network."""

from __future__ import annotations

import logging

import httpx
import pytest
from _home_support import FAKE_TOKEN, HA_URL, FakeHA
from oyster.home.adapter import EntityMissing, HomeAssistantClient, HomeError, clean_state
from oyster.home.config import HomeConfig


def make(ha: FakeHA | None = None, **cfg) -> tuple[HomeAssistantClient, FakeHA]:
    ha = ha or FakeHA()
    config = HomeConfig(**{"url": HA_URL, "token": FAKE_TOKEN, **cfg})
    return HomeAssistantClient(config, transport=httpx.MockTransport(ha)), ha


def test_ping_and_state_and_service():
    client, ha = make()
    client.ping()
    assert client.get_state("light.living_ceiling") == "on"
    assert client.call_service("light", "turn_off", "light.living_ceiling") == "off"
    assert ha.states["light.living_ceiling"] == "off"
    assert ha.service_calls == [("light", "turn_off", "light.living_ceiling")]
    assert ha.paths() == [
        "GET /api/",
        "GET /api/states/light.living_ceiling",
        "POST /api/services/light/turn_off",
    ]


def test_the_token_is_only_sent_as_a_bearer_header():
    client, ha = make()
    client.ping()
    client.call_service("light", "toggle", "light.living_ceiling")
    for r in ha.requests:
        assert r.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        assert FAKE_TOKEN not in str(r.url)
        assert FAKE_TOKEN.encode() not in (r.content or b"")
        assert str(r.url).startswith(HA_URL)


def test_service_answer_without_the_entity_gives_none():
    client, ha = make()
    ha.empty_service_answer = True
    assert client.call_service("light", "turn_on", "light.living_ceiling") is None


def test_unknown_entity_is_a_specific_error():
    client, _ = make()
    with pytest.raises(EntityMissing) as e:
        client.get_state("light.nope")
    assert e.value.code == "home_entity_missing" and e.value.status == 502


@pytest.mark.parametrize(
    ("mode", "code", "status"),
    [
        ("down", "home_unreachable", 503),
        ("tls", "home_unreachable", 503),
        ("timeout", "home_timeout", 503),
        ("401", "home_auth_failed", 502),
        ("403", "home_auth_failed", 502),
        ("400", "home_refused", 502),
        ("500", "home_upstream_error", 502),
        ("redirect", "home_upstream_error", 502),
        ("huge", "home_upstream_error", 502),
        ("garbage", "home_upstream_error", 502),
    ],
)
def test_failures_map_to_clean_errors(mode, code, status, caplog):
    caplog.set_level(logging.DEBUG)
    client, ha = make()
    ha.fail = mode
    with pytest.raises(HomeError) as e:
        client.get_state("light.living_ceiling")
    err = e.value
    assert (err.code, err.status) == (code, status)
    shown = f"{err} {err.detail} {err.code} {err!r}"
    for secret in (FAKE_TOKEN, HA_URL, "192.168.1.50", "203.0.113.9"):
        assert secret not in shown
        assert secret not in caplog.text
    assert err.unknown_outcome == (mode == "timeout")


def test_certificate_failure_is_named_without_the_details():
    client, ha = make()
    ha.fail = "tls"
    with pytest.raises(HomeError) as e:
        client.ping()
    assert "certificate" in e.value.detail


def test_a_redirect_is_never_followed():
    client, ha = make()
    ha.fail = "redirect"
    with pytest.raises(HomeError):
        client.ping()
    assert len(ha.requests) == 1  # the Location header was not requested, so the token went nowhere else


def test_service_call_failures():
    client, ha = make()
    ha.fail, ha.fail_on = "400", "/api/services"
    with pytest.raises(HomeError) as e:
        client.call_service("light", "turn_on", "light.living_ceiling")
    assert e.value.code == "home_refused"


def test_only_a_state_lookup_may_be_missing():
    # a 404 on the ping or on a service call is an unexpected status, not "entity missing"
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    c = HomeAssistantClient(HomeConfig(url=HA_URL, token=FAKE_TOKEN), transport=transport)
    with pytest.raises(HomeError) as e:
        c.ping()
    assert e.value.code == "home_upstream_error"
    with pytest.raises(HomeError) as e2:
        c.call_service("light", "turn_on", "light.x")
    assert e2.value.code == "home_upstream_error"


def test_state_words_are_cleaned_for_the_screen():
    assert clean_state("ON") == "on"
    assert clean_state("armed_away") == "armed_away"
    assert clean_state("x" * 40) == "x" * 16
    assert clean_state("a\nb\x00c") == "a?b?c"
    assert clean_state("Küche") == "k?che"
    assert clean_state("") == "unknown"
    assert clean_state(None) == "unknown"
    assert clean_state(5) == "unknown"


def test_a_base_path_in_the_url_is_kept():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"message": "API running."})

    client = HomeAssistantClient(
        HomeConfig(url="http://192.168.1.50:8123/ha", token=FAKE_TOKEN),
        transport=httpx.MockTransport(handler),
    )
    client.ping()
    assert seen == ["/ha/api/"]


def test_a_missing_ca_file_fails_at_construction():
    with pytest.raises(OSError):
        HomeAssistantClient(
            HomeConfig(url="https://ha.example.test", token=FAKE_TOKEN, ca_file="/nonexistent/ca.pem")
        )
