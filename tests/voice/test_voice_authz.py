"""Authentication and session ownership: voice routes are device routes with the usual token rules, and an
upload is bound to the device that owns the session."""

from __future__ import annotations

from _voice_support import ADMIN, SESSION, VoiceKit, code_of, ulaw_audio


def test_begin_requires_a_token(kit: VoiceKit):
    r = kit.client.post(f"/v1/sessions/{SESSION}/voice", json={"format": "ulaw_8k", "max_seconds": 12})
    assert r.status_code == 401


def test_owner_token_is_refused_on_the_device_listener(kit: VoiceKit):
    dev = kit.device_app()
    r = dev.post(
        f"/v1/sessions/{SESSION}/voice",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={"format": "ulaw_8k", "max_seconds": 12},
    )
    assert r.status_code == 403


def test_owner_token_cannot_record_on_loopback_either(kit: VoiceKit):
    # Voice is device-only: the owner token is the wrong role (device routes require a device).
    r = kit.begin(headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 403
    assert code_of(r) == "forbidden"


def test_a_session_owned_by_another_device_is_not_found(kit: VoiceKit):
    other = kit.store.create_device("dev-b", "dev-b", False)
    r = kit.begin(headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404
    assert code_of(r) == "not_found"
    assert kit.runtime.store.live_count() == 0


def test_unknown_session_is_not_found(kit: VoiceKit):
    r = kit.client.post(
        "/v1/sessions/sess-doesnotexist/voice",
        headers=kit.headers,
        json={"format": "ulaw_8k", "max_seconds": 12},
    )
    assert r.status_code == 404


def test_device_cannot_touch_another_devices_upload(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(4096))
    other = kit.store.create_device("dev-b", "dev-b", False)
    kit.store.register_session("dev-b", "sess-voiceother1", "live", [], None)
    # dev-b points at dev-a's session id -> 404 at the ownership check, before the upload is seen
    r = kit.client.post(
        f"/v1/sessions/{SESSION}/voice/{uid}/finish",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert r.status_code == 404
