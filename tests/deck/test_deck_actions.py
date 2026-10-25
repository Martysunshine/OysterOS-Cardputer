"""Allowlist: typed actions only, fixed impact, nothing shell-like anywhere in the package."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from oyster.deck.actions import (
    ACTION_MODELS,
    ALLOWLIST,
    MEDIA_KEYS,
    ActionFailed,
    ActionOutcomeUnknown,
    ActionRejected,
    ActionTimeout,
    MockExecutor,
    ServiceRestart,
    is_impactful,
    parse_action,
)
from oyster.deck.profiles import ProfileInvalid, load_profile

PACKAGE = Path(__file__).resolve().parents[2] / "backend" / "oyster" / "deck"
EXPECTED_IMPACT = {
    "noop": False,
    "host_health_check": False,
    "service_status": False,
    "wake_on_lan": True,
    "service_restart": True,
    "media_key": False,
    "lock_workstation": True,
}
SAMPLE = {
    "noop": {},
    "host_health_check": {"host_id": "h1"},
    "service_status": {"host_id": "h1", "service": "nginx"},
    "wake_on_lan": {"host_id": "h1"},
    "service_restart": {"host_id": "h1", "service": "nginx"},
    "media_key": {"host_id": "h1", "key": "play_pause"},
    "lock_workstation": {"host_id": "h1"},
}


def test_allowlist_is_exactly_the_documented_set():
    assert dict.fromkeys(ALLOWLIST, None).keys() == EXPECTED_IMPACT.keys()
    assert len(ACTION_MODELS) == len(ALLOWLIST)


@pytest.mark.parametrize("name,impactful", EXPECTED_IMPACT.items())
def test_impact_is_fixed_per_action_type(name, impactful):
    action = parse_action({"type": name, **SAMPLE[name]})
    assert is_impactful(action) is impactful
    assert ALLOWLIST[name].impactful is impactful


@pytest.mark.parametrize("name", EXPECTED_IMPACT)
def test_impact_cannot_be_supplied_or_overridden(name):
    """`impactful` is a class constant, not a field: supplying it is an unknown field."""
    model = ALLOWLIST[name]
    assert "impactful" not in model.model_fields
    for value in (True, False):
        with pytest.raises(ActionRejected):
            parse_action({"type": name, **SAMPLE[name], "impactful": value})
    with pytest.raises(Exception):  # noqa: B017 - frozen model: the attribute cannot be assigned either
        parse_action({"type": name, **SAMPLE[name]}).impactful = False


@pytest.mark.parametrize(
    "raw", [{"type": "rm"}, {"type": "shell", "cmd": "id"}, {}, {"type": 7}, "noop", None]
)
def test_unknown_action_types_are_rejected(raw):
    with pytest.raises(ActionRejected):
        parse_action(raw)


def test_unknown_action_type_is_rejected_when_a_profile_is_defined(kit):
    profile = {
        "profile_id": "bad",
        "name": "Bad",
        "device_ids": ["dev-a"],
        "slots": {"1": {"label": "x", "action": {"type": "run_script", "path": "/tmp/x"}}},
    }
    r = kit.client.put("/v1/deck/profiles/bad", headers=kit.owner, json=profile)
    assert r.status_code == 422
    assert r.json()["error"] == "action_not_allowed"
    assert "run_script" not in r.text  # the rejected input is not echoed
    assert kit.client.get("/v1/deck/profiles/bad", headers=kit.owner).status_code == 404
    with pytest.raises(ProfileInvalid) as e:
        load_profile(profile)
    assert e.value.reason == "action_not_allowed"


def test_unknown_action_type_in_a_stored_profile_is_rejected_when_the_slot_runs(kit):
    """A stored row that no longer passes the allowlist (tampering, downgrade) never produces a job."""
    import json

    with kit.runtime.store.tx() as conn:
        body = json.loads(conn.execute("SELECT body FROM profiles WHERE profile_id='dev'").fetchone()[0])
        body["slots"]["1"]["actions"] = [{"type": "run_script", "path": "/bin/true"}]
        conn.execute("UPDATE profiles SET body=? WHERE profile_id='dev'", (json.dumps(body),))
    r = kit.run(kit.dev_a, 2, "key-tamper-1")
    assert r.status_code == 409 and r.json()["error"] == "action_not_allowed"
    assert kit.drain() == 0 and kit.executor.calls == []
    assert kit.client.get("/v1/deck/jobs", headers=kit.owner).json()["jobs"] == []
    reasons = [row[7] for row in kit.audit_rows() if row[5] == "request.denied"]
    assert reasons == ["action_not_allowed"]


def test_a_tampered_job_row_is_refused_by_the_runner(kit):
    """The allowlist is applied once more right before execution."""
    import json

    r = kit.run(kit.dev_a, 2, "key-tamper-2")
    job_id = r.json()["job_id"]
    with kit.runtime.store.tx() as conn:
        conn.execute(
            "UPDATE jobs SET actions=? WHERE job_id=?",
            (json.dumps([{"type": "run_script", "path": "/bin/true"}]), job_id),
        )
    kit.drain()
    j = kit.job(kit.dev_a, job_id).json()
    assert j["state"] == "failed" and j["reason"] == "action_not_allowed"
    assert kit.executor.calls == []


@pytest.mark.parametrize(
    "extra",
    [{"command": "id"}, {"args": ["-rf"]}, {"shell": True}, {"script": "x.sh"}, {"timeout_s": 9999}],
)
def test_extra_fields_are_rejected(extra):
    for name, base in SAMPLE.items():
        with pytest.raises(ActionRejected):
            parse_action({"type": name, **base, **extra})


COMMON_BAD_NAMES = ["", "a b", "x;rm -rf /", "$(id)", "h\n1", "../etc", "h1 && id", "h`id`"]


@pytest.mark.parametrize("bad", [*COMMON_BAD_NAMES, "a" * 41])
def test_host_ids_are_validated_names(bad):
    with pytest.raises(ActionRejected):
        parse_action({"type": "host_health_check", "host_id": bad})


@pytest.mark.parametrize("bad", [*COMMON_BAD_NAMES, "a" * 65, "-x"])
def test_service_names_are_validated_names(bad):
    with pytest.raises(ActionRejected):
        parse_action({"type": "service_status", "host_id": "h1", "service": bad})


def test_missing_arguments_are_rejected():
    for name in ("host_health_check", "service_status", "wake_on_lan", "service_restart", "media_key"):
        with pytest.raises(ActionRejected):
            parse_action({"type": name})
    with pytest.raises(ActionRejected):
        parse_action({"type": "media_key", "host_id": "h1"})  # the key is required
    with pytest.raises(ActionRejected):
        parse_action({"type": "lock_workstation"})


@pytest.mark.parametrize("key", MEDIA_KEYS)
def test_every_media_key_is_accepted(key):
    action = parse_action({"type": "media_key", "host_id": "h1", "key": key})
    assert action.key == key and not is_impactful(action)


def test_the_media_keys_are_exactly_the_documented_set():
    assert MEDIA_KEYS == ("play_pause", "next", "prev", "vol_up", "vol_down", "mute")


@pytest.mark.parametrize(
    "bad", ["stop", "PLAY_PAUSE", "play", "", " mute", "mute ", "volume_up", 3, None, ["mute"], "a;b"]
)
def test_unknown_media_keys_are_rejected(bad):
    with pytest.raises(ActionRejected):
        parse_action({"type": "media_key", "host_id": "h1", "key": bad})


def test_locking_is_impactful_and_pressing_a_media_key_is_not():
    """Both new types are covered by the confirmation rule through their class constants."""
    assert ALLOWLIST["lock_workstation"].impactful is True
    assert ALLOWLIST["media_key"].impactful is False
    assert ALLOWLIST["lock_workstation"].timeout_s == 15


def test_no_shell_or_exec_like_field_exists_on_any_action_model():
    allowed_fields = {"type", "host_id", "service", "key"}
    bad_words = re.compile(r"cmd|command|shell|exec|script|argv|args|eval|path|url|param|template|env", re.I)
    for model in ACTION_MODELS:
        assert model.model_config.get("extra") == "forbid"
        assert set(model.model_fields) <= allowed_fields
        assert not [f for f in model.model_fields if bad_words.search(f)]
        for field in model.model_fields.values():
            # every string argument is pattern-bounded (names, not free text)
            if field.annotation is str and field is not model.model_fields["type"]:
                assert any(getattr(m, "pattern", None) for m in field.metadata)


# The one module that talks to the operating system (Win32 calls through ctypes); every other file of the
# package must stay free of it.
OS_MODULE = "os_windows.py"
CTYPES = re.compile(r"\bctypes\b")


def test_package_source_has_no_shell_exec_or_eval():
    forbidden = re.compile(
        r"\b(subprocess|os\.system|os\.popen|popen|shlex|pty|ctypes)\b|\beval\(|\bexec\(|shell\s*=|"
        r"__import__|create_subprocess|importlib",
        re.I,
    )
    hits = []
    for path in sorted(PACKAGE.glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if path.name == OS_MODULE:
                line = CTYPES.sub("", line)  # ctypes is the OS layer's tool; everything else stays forbidden
            if forbidden.search(line):
                hits.append(f"{path.name}:{n}: {line.strip()}")
    assert hits == []


def test_only_the_os_layer_uses_ctypes():
    users = [p.name for p in sorted(PACKAGE.glob("*.py")) if CTYPES.search(p.read_text(encoding="utf-8"))]
    assert users == [OS_MODULE]


def test_timeouts_are_bounded_and_impactful_ones_are_documented_ceilings():
    assert all(0 < m.timeout_s <= 60 for m in ACTION_MODELS)
    assert ServiceRestart.timeout_s == 60


def test_mock_executor_records_calls_and_executes_nothing():
    ex = MockExecutor()
    result = ex.execute(parse_action({"type": "noop"}))
    assert result.ok and "nothing was executed" in result.summary
    assert [type(c).__name__ for c in ex.calls] == ["Noop"]
    assert ex.kind == "mock"


def test_mock_executor_injects_failures_timeouts_and_lost_results():
    ex = MockExecutor()
    noop = parse_action({"type": "noop"})
    for kind, exc in (("fail", ActionFailed), ("timeout", ActionTimeout), ("lost", ActionOutcomeUnknown)):
        ex.inject("noop", kind)
        with pytest.raises(exc):
            ex.execute(noop)
    ex.inject("noop", "error")
    with pytest.raises(RuntimeError):
        ex.execute(noop)
    assert ex.execute(noop).ok  # injections are consumed
    with pytest.raises(ValueError):
        ex.inject("noop", "bogus")
    assert len(ex.calls) == 5
