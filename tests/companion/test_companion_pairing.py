"""Pairing: single-use, expiring, lock-out, throttled, hashed at rest, never takes over an existing device."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from _companion_support import OWNER, Clock, Rig
from oyster.companion.audit import AuditLog
from oyster.companion.models import PairingCreate, PairingRedeem
from oyster.companion.pairing import (
    CODE_ALPHABET,
    CODE_LENGTH,
    MAX_FAILED_ATTEMPTS,
    PAIRING_TTL_S,
    DeviceExists,
    PairingRejected,
    PairingService,
    PairingUnavailable,
)
from oyster.companion.store import CompanionStore, PairingRow
from oyster.store import NotFound

GENERIC = {"error": "unauthorized", "detail": "invalid or expired pairing code"}


def wrong(code: str) -> str:
    return ("A" if code[0] != "A" else "B") + code[1:]


def test_the_code_is_short_unambiguous_and_expires_in_300_s(rig: Rig):
    created = rig.new_code()
    code = created["code"]
    assert len(code) == CODE_LENGTH == 8
    assert set(code) <= set(CODE_ALPHABET)
    assert not set("0O1ILU") & set(CODE_ALPHABET)
    assert created["ttl_s"] == PAIRING_TTL_S == 300
    assert created["expires_at"] == rig.clock.t + 300
    assert created["pairing_id"].startswith("pair-")


def test_codes_come_from_the_injected_rng(tmp_path: Path):
    def code_for(seed: int, name: str) -> str:
        store = CompanionStore(tmp_path / f"{name}.db")
        return PairingService(store, AuditLog(store, Clock()), Clock(), random.Random(seed)).create(
            PairingCreate()
        ).code

    assert code_for(5, "a") == code_for(5, "b") != code_for(6, "c")


def test_redeeming_returns_a_working_device_token_once(rig: Rig):
    rig.host("host-1", device_ids=["dev-9"])
    created = rig.new_code(simulated=True)
    r = rig.redeem(created["code"], "dev-9", display_name="My Cardputer")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"device_id", "token"} and body["device_id"] == "dev-9"
    auth = {"Authorization": f"Bearer {body['token']}"}
    assert [h["host_id"] for h in rig.c.get("/v1/hosts", headers=auth).json()["hosts"]] == ["host-1"]
    dev = rig.store.get_device("dev-9")
    assert (dev.display_name, dev.simulated) == ("My Cardputer", True)
    assert rig.store.device_for_token(body["token"]).device_id == "dev-9"  # the existing device auth


def test_code_input_is_case_insensitive(rig: Rig):
    code = rig.new_code()["code"]
    assert rig.redeem(code.lower()).status_code == 200


def test_expired_code(rig: Rig, clock: Clock):
    code = rig.new_code()["code"]
    clock.advance(PAIRING_TTL_S)
    r = rig.redeem(code)
    assert r.status_code == 401 and r.json() == GENERIC
    assert "reason=expired" in rig.audit()[0]["detail"]


def test_code_still_valid_just_before_expiry(rig: Rig, clock: Clock):
    code = rig.new_code()["code"]
    clock.advance(PAIRING_TTL_S - 1)
    assert rig.redeem(code).status_code == 200


def test_reuse_is_refused_and_does_not_disturb_the_first_device(rig: Rig):
    code = rig.new_code()["code"]
    first = rig.redeem(code, "dev-9")
    second = rig.redeem(code, "dev-8")
    assert first.status_code == 200
    assert second.status_code == 401 and second.json() == GENERIC
    auth = {"Authorization": f"Bearer {first.json()['token']}"}
    assert rig.c.get("/v1/hosts", headers=auth).status_code == 200
    with pytest.raises(NotFound):
        rig.store.get_device("dev-8")
    assert "reason=reused" in rig.audit()[0]["detail"]


def test_wrong_code_gives_the_same_answer_as_every_other_failure(rig: Rig):
    assert rig.redeem("ABCDEFGH").json() == GENERIC  # no code exists at all
    code = rig.new_code()["code"]
    bad = rig.redeem(wrong(code))
    assert bad.status_code == 401 and bad.json() == GENERIC
    assert "www-authenticate" not in bad.headers


def test_lockout_after_five_failed_attempts(rig: Rig):
    code = rig.new_code()["code"]
    for n in range(1, MAX_FAILED_ATTEMPTS + 1):
        assert rig.redeem(wrong(code)).status_code == 401, n
    # the code is burned: even the right one no longer works
    r = rig.redeem(code)
    assert r.status_code == 401 and r.json() == GENERIC
    entries = rig.audit()
    assert "reason=burned" in entries[0]["detail"]
    assert entries[1]["outcome"] == "burned"
    assert "attempts=5" in entries[1]["detail"]
    with pytest.raises(NotFound):
        rig.store.get_device("dev-9")


def test_four_failures_do_not_burn_the_code(rig: Rig):
    code = rig.new_code()["code"]
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert rig.redeem(wrong(code)).status_code == 401
    assert rig.redeem(code).status_code == 200


def test_a_new_code_burns_the_previous_one(rig: Rig):
    old = rig.new_code()["code"]
    new = rig.new_code()["code"]
    assert old != new
    assert rig.redeem(old).status_code == 401
    assert rig.redeem(new).status_code == 200


def test_a_bound_code_only_works_for_its_device_id(rig: Rig):
    created = rig.new_code(device_id="dev-5")
    assert created["device_id"] == "dev-5"
    assert rig.redeem(created["code"], "dev-6").status_code == 401
    assert rig.redeem(created["code"], "dev-5").status_code == 200


def test_pairing_never_takes_over_an_existing_device(rig: Rig):
    old = rig.device("dev-1")
    code = rig.new_code()["code"]
    r = rig.redeem(code, "dev-1")
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert rig.c.get("/v1/hosts", headers=old).status_code == 200  # old token not rotated
    assert rig.redeem(code, "dev-2").status_code == 200  # the code was not consumed by the refusal


def test_redemption_attempts_are_throttled_per_client(rig: Rig, clock: Clock):
    for n in range(11):
        assert rig.redeem("ABCDEFGH").status_code == 401, n
    blocked = rig.redeem("ABCDEFGH")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1
    code = rig.new_code()["code"]
    assert rig.redeem(code).status_code == 429  # a right code does not bypass the throttle
    clock.advance(61)
    assert rig.redeem(code).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"code": "ABCDEFGH"},
        {"device_id": "dev-9"},
        {"code": "SHORT", "device_id": "dev-9"},
        {"code": "ABCDEFGHJ", "device_id": "dev-9"},
        {"code": "ABCD EFG", "device_id": "dev-9"},
        {"code": "ABCDEFGH", "device_id": "bad id"},
        {"code": "ABCDEFGH", "device_id": "dev-9", "command": "id"},
        {"code": "ABCDEFGH", "device_id": "dev-9", "token": "x"},
        {"code": 12345678, "device_id": "dev-9"},
    ],
)
def test_redemption_validates_its_body(rig: Rig, body):
    assert rig.c.post("/v1/pair", json=body).status_code == 422


def test_redemption_body_is_capped_at_4_kib(rig: Rig):
    r = rig.c.post("/v1/pair", json={"code": "ABCDEFGH", "device_id": "dev-9", "display_name": "x" * 5000})
    assert r.status_code == 413


def test_creating_a_code_validates_its_body(rig: Rig):
    for body in ({"command": "id"}, {"device_id": "bad id"}, {"simulated": "maybe"}, []):
        assert rig.c.post("/v1/admin/pairing", headers=OWNER, json=body).status_code == 422


def test_the_plaintext_code_is_never_stored(rig: Rig):
    created = rig.new_code()
    code = created["code"]
    token = rig.redeem(code).json()["token"]
    rig.redeem(wrong(code))
    db = rig.settings.data_dir / "companion.db"
    dump = "\n".join(rig.raw_companion_db().iterdump())
    raw = b"".join(p.read_bytes() for p in db.parent.glob("companion.db*"))
    for secret in (code, code.lower(), token):
        assert secret not in dump
        assert secret.encode() not in raw
    row = rig.raw_companion_db().execute("SELECT code_salt, code_hash FROM pairing").fetchone()
    assert len(row[0]) == 32 and len(row[1]) == 64 and row[1] != code
    assert code not in str(rig.audit()) and token not in str(rig.audit())


def test_equal_codes_get_different_salts_and_hashes(tmp_path: Path):
    store = CompanionStore(tmp_path / "c.db")
    svc = PairingService(store, AuditLog(store, Clock()), Clock(), random.Random(3))
    svc.create(PairingCreate())
    first = store.latest_pairing()
    svc.create(PairingCreate())
    second = store.latest_pairing()
    assert first.code_salt != second.code_salt and first.code_hash != second.code_hash


# --- unit level: the claim is atomic, and a weak device store fails closed ---------------------------------
def test_a_code_can_be_claimed_exactly_once(tmp_path: Path):
    store = CompanionStore(tmp_path / "c.db")
    row = PairingRow("pair-1", "00", "11", None, False, 100.0, 400.0, 0, None, None, None)
    store.create_pairing(row)
    assert store.claim_pairing("pair-1", "dev-1", 101.0) is True
    assert store.claim_pairing("pair-1", "dev-2", 102.0) is False
    other = CompanionStore(tmp_path / "c.db")  # a second listener on the same file
    assert other.claim_pairing("pair-1", "dev-3", 103.0) is False


def test_an_expired_code_cannot_be_claimed(tmp_path: Path):
    store = CompanionStore(tmp_path / "c.db")
    store.create_pairing(PairingRow("pair-1", "00", "11", None, False, 100.0, 400.0, 0, None, None, None))
    assert store.claim_pairing("pair-1", "dev-1", 400.0) is False


class _StoreWithoutLookup:
    def __init__(self):
        self.created = []

    def create_device(self, device_id, display_name, simulated):
        self.created.append(device_id)
        return "token"


class _StoreThatFails:
    def get_device(self, device_id):
        raise NotFound(device_id)

    def create_device(self, device_id, display_name, simulated):
        raise RuntimeError("disk on fire")


def _service(tmp_path: Path):
    store = CompanionStore(tmp_path / "c.db")
    audit = AuditLog(store, Clock())
    return PairingService(store, audit, Clock(), random.Random(2)), audit


def test_a_device_store_that_cannot_check_ids_fails_closed(tmp_path: Path):
    svc, _ = _service(tmp_path)
    code = svc.create(PairingCreate()).code
    devices = _StoreWithoutLookup()
    with pytest.raises(PairingUnavailable):
        svc.redeem(PairingRedeem(code=code, device_id="dev-1"), devices)
    assert devices.created == []


def test_a_failing_enrolment_is_reported_without_details(tmp_path: Path):
    svc, audit = _service(tmp_path)
    code = svc.create(PairingCreate()).code
    with pytest.raises(PairingUnavailable):
        svc.redeem(PairingRedeem(code=code, device_id="dev-1"), _StoreThatFails())
    assert "disk on fire" not in str(audit.entries())
    with pytest.raises(PairingRejected):  # the code was claimed before enrolment: the owner issues a new one
        svc.redeem(PairingRedeem(code=code, device_id="dev-1"), _StoreThatFails())


def test_device_exists_is_signalled_without_consuming_the_code(tmp_path: Path):
    svc, _ = _service(tmp_path)
    code = svc.create(PairingCreate()).code

    class _Has:
        def get_device(self, device_id):
            return object()

    with pytest.raises(DeviceExists):
        svc.redeem(PairingRedeem(code=code, device_id="dev-1"), _Has())
