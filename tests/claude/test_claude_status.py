"""The usage line: the hook-side report, the device read, retention of the last 8 sessions and bounds."""

from __future__ import annotations

import pytest


def report(kit, **kw):
    body = {"session": "sess-1", "model": "Opus", "cost_usd": 0.5, **kw}
    return kit.client.post("/v1/claude/status", headers=kit.owner, json=body)


def read(kit):
    return kit.client.get("/v1/claude/status", headers=kit.dev_a).json()


def test_nothing_reported_means_not_configured(kit):
    assert read(kit) == {"configured": False}


def test_the_newest_report_is_returned_with_its_age(kit):
    assert report(kit, context_pct=8.04, cost_usd=0.01234).status_code == 200
    kit.clock.advance(7)
    assert read(kit) == {
        "configured": True,
        "updated_s_ago": 7,
        "model": "Opus",
        "cost_usd": 0.0123,
        "context_pct": 8.0,
        "five_hour_pct": None,
        "seven_day_pct": None,
    }


def test_plan_limits_are_passed_through_only_when_given(kit):
    report(kit, five_hour_pct=23.46, seven_day_pct=41.2)
    out = read(kit)
    assert out["five_hour_pct"] == 23.5 and out["seven_day_pct"] == 41.2 and out["context_pct"] is None
    report(kit)  # a later report without them clears them: nothing is carried over or guessed
    out = read(kit)
    assert out["five_hour_pct"] is None and out["seven_day_pct"] is None


def test_the_same_session_is_updated_in_place(kit):
    report(kit, cost_usd=1.0)
    kit.clock.advance(5)
    report(kit, cost_usd=2.0)
    assert read(kit)["cost_usd"] == 2.0 and read(kit)["updated_s_ago"] == 0
    assert kit.runtime.store.count_status() == 1


def test_the_newest_session_wins_and_only_eight_are_kept(kit):
    for i in range(11):
        report(kit, session=f"sess-{i}", model=f"M{i}", cost_usd=float(i))
        kit.clock.advance(1)
    assert kit.runtime.store.count_status() == 8
    out = read(kit)
    assert out["model"] == "M10" and out["cost_usd"] == 10.0
    with kit.runtime.store.tx() as conn:
        kept = {r[0] for r in conn.execute("SELECT session FROM status")}
    assert kept == {f"sess-{i}" for i in range(3, 11)}


def test_an_older_session_reporting_again_becomes_the_newest(kit):
    report(kit, session="a", model="A")
    kit.clock.advance(1)
    report(kit, session="b", model="B")
    kit.clock.advance(1)
    report(kit, session="a", model="A2")
    assert read(kit)["model"] == "A2"


@pytest.mark.parametrize(
    "patch",
    [
        {"cost_usd": -0.01},
        {"cost_usd": 1_000_001},
        {"cost_usd": "1.5"},
        {"cost_usd": None},
        {"context_pct": -1},
        {"context_pct": 100.5},
        {"five_hour_pct": 101},
        {"seven_day_pct": -3},
        {"model": ""},
        {"model": "m" * 41},
        {"model": "café"},
        {"model": "line\nbreak"},
        {"session": ""},
        {"session": "s" * 65},
        {"session": "bad session"},
        {"surprise": 1},
    ],
)
def test_out_of_bounds_reports_are_rejected(kit, patch):
    assert report(kit, **patch).status_code == 422
    assert read(kit) == {"configured": False}


def test_nan_and_infinity_are_rejected(kit):
    for raw in (
        b'{"session":"s","model":"m","cost_usd":NaN}',
        b'{"session":"s","model":"m","cost_usd":Infinity}',
    ):
        r = kit.client.post(
            "/v1/claude/status", headers={**kit.owner, "Content-Type": "application/json"}, content=raw
        )
        assert r.status_code == 422
    assert read(kit) == {"configured": False}


def test_the_model_string_is_redacted_too(kit):
    report(kit, model="x api_key=abc123fake")
    assert "abc123fake" not in read(kit)["model"]


def test_status_survives_a_restart(kit):
    report(kit, cost_usd=3.25, context_pct=40)
    kit.restart()
    out = read(kit)
    assert out["configured"] is True and out["cost_usd"] == 3.25 and out["context_pct"] == 40.0


def test_the_device_reply_is_small(kit):
    report(
        kit,
        model="m" * 40,
        session="s" * 64,
        cost_usd=999999.9999,
        context_pct=99.9,
        five_hour_pct=99.9,
        seven_day_pct=99.9,
    )
    assert len(kit.client.get("/v1/claude/status", headers=kit.dev_a).content) < 400
