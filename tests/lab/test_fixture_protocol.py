"""Fixture protocol state machine (no Bluetooth): auth exchange, modes, transfer, virtual LED."""

import hashlib
import logging
import struct

import pytest
from oyster_lab.fixture.profile import load_profile
from oyster_lab.fixture.protocol import (
    ATT_INSUFFICIENT_AUTHORIZATION,
    ATT_INVALID_ATTRIBUTE_VALUE_LENGTH,
    ATT_INVALID_OFFSET,
    ATT_READ_NOT_PERMITTED,
    ATT_VALUE_NOT_ALLOWED,
    ATT_WRITE_NOT_PERMITTED,
    CHALLENGE_TTL_S,
    CHUNK_HEADER,
    CTRL,
    FixtureState,
    Mode,
    auth_response,
    chunk_len_for_mtu,
)
from oyster_lab.synthetic import APP_DENIAL, ManifestDenied, parse_manifest

TOKEN = b"lab-test-token-0123456789abcdef-not-a-real-secret"
PROFILE = load_profile()
ST = PROFILE.status_codes
OPS = PROFILE.ctrl_ops


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def det_random():
    counter = [0]

    def rnd(n: int) -> bytes:
        counter[0] += 1
        return hashlib.sha256(f"rng{counter[0]}".encode()).digest()[:n]

    return rnd


def make(mode, clock=None, **kw) -> FixtureState:
    return FixtureState(PROFILE, mode, TOKEN, clock=clock or Clock(), random_bytes=det_random(), **kw)


def read(state, name, offset=0) -> bytes:
    r = state.handle_read(name, offset)
    assert r.att_error is None, r
    return r.value


def authorize(state, key=TOKEN) -> None:
    v = read(state, "app_auth")
    assert len(v) == 17
    assert state.handle_write("app_auth", auth_response(key, v[1:])).att_error is None


def ctrl(op, tag, offset=0, length=0, seq=0) -> bytes:
    return CTRL.pack(op, tag, offset, length, seq)


def chunk(state):
    v = read(state, "chunk_data")
    status, seq, off, n = CHUNK_HEADER.unpack(v[:9])
    assert len(v) == 9 + n
    return status, seq, off, n, v[9:]


def diag_status(state):
    return struct.unpack("<BH", read(state, "diag_status"))


ALL_MODES = [Mode.VULNERABLE, Mode.CORRECTED]


# ----- identity / mode blindness -----
@pytest.mark.parametrize("mode", ALL_MODES)
def test_device_info_is_profile_identity(mode):
    assert read(make(mode), "device_info") == b"oyster-lab-v1;1"


def test_mode_never_exposed_over_gatt():
    readable = [n for n, s in PROFILE.characteristics.items() if "read" in s.properties]
    for authorized in (False, True):
        views = {}
        for mode in ALL_MODES:
            st = make(mode)
            if authorized:
                authorize(st)
            views[mode] = {n: read(st, n) for n in readable if n not in ("diag_manifest", "chunk_data")}
            for v in views[mode].values():
                assert b"vulnerable" not in v.lower() and b"corrected" not in v.lower()
        assert views[Mode.VULNERABLE] == views[Mode.CORRECTED]
    # once authorized, the protected values are identical too
    a, b = make(Mode.VULNERABLE), make(Mode.CORRECTED)
    authorize(a)
    authorize(b)
    assert read(a, "diag_manifest") == read(b, "diag_manifest")


# ----- app_auth -----
def test_auth_good_exchange():
    st = make(Mode.CORRECTED)
    assert read(st, "app_auth")[0] == 0
    authorize(st)
    assert st.is_authorized()
    assert read(st, "app_auth")[0] == 1


def test_auth_bad_response_rejected():
    st = make(Mode.CORRECTED)
    v = read(st, "app_auth")
    assert st.handle_write("app_auth", auth_response(b"x" * 32, v[1:])).att_error is None
    assert not st.is_authorized()
    assert read(st, "app_auth")[0] == 0


def test_auth_challenge_single_use_and_replaced_by_new_read():
    st = make(Mode.CORRECTED)
    old = read(st, "app_auth")[1:]
    new = read(st, "app_auth")[1:]
    assert old != new
    st.handle_write("app_auth", auth_response(TOKEN, old))  # stale challenge
    assert not st.is_authorized()
    # no outstanding challenge now: even the right answer to `new` is refused (single use)
    st.handle_write("app_auth", auth_response(TOKEN, new))
    assert not st.is_authorized()


def test_auth_replay_after_reset_rejected():
    st = make(Mode.CORRECTED)
    ch = read(st, "app_auth")[1:]
    resp = auth_response(TOKEN, ch)
    st.handle_write("app_auth", resp)
    assert st.is_authorized()
    st.reset()
    assert not st.is_authorized()
    st.handle_write("app_auth", resp)
    assert not st.is_authorized()


def test_auth_expires_after_ttl():
    clock = Clock()
    st = make(Mode.CORRECTED, clock)
    authorize(st)
    clock.t += PROFILE.auth_ttl_s - 0.1
    assert st.is_authorized()
    clock.t += 0.2
    assert not st.is_authorized()
    assert read(st, "app_auth")[0] == 0
    assert read(st, "diag_manifest") == APP_DENIAL


def test_auth_challenge_expires():
    clock = Clock()
    st = make(Mode.CORRECTED, clock)
    ch = read(st, "app_auth")[1:]
    clock.t += CHALLENGE_TTL_S + 1
    st.handle_write("app_auth", auth_response(TOKEN, ch))
    assert not st.is_authorized()


def test_failed_attempt_keeps_existing_authorization():
    st = make(Mode.CORRECTED)
    authorize(st)
    v = read(st, "app_auth")
    st.handle_write("app_auth", b"\x00" * 32)
    assert v[0] == 1 and st.is_authorized()


def test_auth_wrong_length_and_long_write_reassembly():
    st = make(Mode.CORRECTED)
    assert st.handle_write("app_auth", b"\x00" * 33).att_error == ATT_INVALID_ATTRIBUTE_VALUE_LENGTH
    resp = auth_response(TOKEN, read(st, "app_auth")[1:])
    assert st.handle_write("app_auth", resp[:18], offset=0).att_error is None  # held, not processed
    assert not st.is_authorized()
    assert st.handle_write("app_auth", resp[18:], offset=18).att_error is None
    assert st.is_authorized()
    assert st.handle_write("app_auth", b"\x00" * 4, offset=5).att_error == ATT_INVALID_OFFSET


def test_reset_clears_authorization_and_state():
    st = make(Mode.CORRECTED)
    authorize(st)
    assert st.handle_write("virtual_led", b"\x02").att_error is None
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 10, 7))
    st.reset()
    assert not st.is_authorized()
    assert read(st, "virtual_led") == b"\x00"
    assert diag_status(st) == (ST["no_request"], 0)


def test_token_must_be_32_bytes():
    with pytest.raises(ValueError):
        FixtureState(PROFILE, Mode.CORRECTED, b"short")


def test_logs_never_contain_secrets(caplog):
    caplog.set_level(logging.DEBUG, logger="oyster_lab.fixture")
    st = make(Mode.CORRECTED)
    ch = read(st, "app_auth")[1:]
    resp = auth_response(TOKEN, ch)
    st.handle_write("app_auth", resp)
    st.handle_write("app_auth", b"\x11" * 32)
    text = caplog.text
    assert "authorized" in text
    for secret in (TOKEN.decode(), resp.hex(), ch.hex(), resp.hex().upper()):
        assert secret not in text


# ----- diag_manifest -----
def test_manifest_corrected_denied_before_auth_returned_after():
    st = make(Mode.CORRECTED)
    assert read(st, "diag_manifest") == APP_DENIAL
    with pytest.raises(ManifestDenied):
        parse_manifest(read(st, "diag_manifest"))
    authorize(st)
    m = parse_manifest(read(st, "diag_manifest"), max_object_bytes=PROFILE.max_object_bytes)
    assert m.object_id == PROFILE.object_id
    assert m.declared_length == len(st.object_bytes)
    assert m.sha256 == hashlib.sha256(st.object_bytes).digest()


def test_manifest_vulnerable_returned_without_auth():
    st = make(Mode.VULNERABLE)
    v = read(st, "diag_manifest")
    assert len(v) == 55 and v == st.manifest


def test_manifest_long_read_offsets():
    st = make(Mode.VULNERABLE)
    assert read(st, "diag_manifest", 22) == st.manifest[22:]
    assert read(st, "diag_manifest", 55) == b""
    assert st.handle_read("diag_manifest", 56).att_error == ATT_INVALID_OFFSET


def test_app_auth_blob_read_keeps_snapshot():
    st = make(Mode.CORRECTED)
    full = read(st, "app_auth")
    assert read(st, "app_auth", 5) == full[5:]


# ----- chunks -----
def test_chunks_corrected_denied_before_auth():
    st = make(Mode.CORRECTED)
    assert st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 100, 1)).att_error is None
    status, seq, _off, n, payload = chunk(st)
    assert (status, seq, n, payload) == (ST["not_authorized"], 1, 0, b"")
    assert diag_status(st) == (ST["not_authorized"], 1)


def test_chunks_corrected_auth_check_precedes_tag_check():
    st = make(Mode.CORRECTED)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], b"\x00\x00\x00\x00", 0, 10, 2))
    assert chunk(st)[0] == ST["not_authorized"]


def test_chunks_vulnerable_returned_without_auth():
    st = make(Mode.VULNERABLE)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 100, 50, 3))
    status, seq, off, n, payload = chunk(st)
    assert (status, seq, off, n) == (ST["ok"], 3, 100, 50)
    assert payload == st.object_bytes[100:150]
    assert diag_status(st) == (ST["ok"], 3)


def test_chunk_response_withdrawn_when_authorization_expires():
    clock = Clock()
    st = make(Mode.CORRECTED, clock)
    authorize(st)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 64, 9))
    assert chunk(st)[0] == ST["ok"]
    clock.t += PROFILE.auth_ttl_s + 1
    status, seq, _off, n, payload = chunk(st)
    assert (status, seq, n, payload) == (ST["not_authorized"], 9, 0, b"")


def _transfer(st, mtu):
    m = parse_manifest(read(st, "diag_manifest"))
    clen = chunk_len_for_mtu(m.chunk_max, mtu)
    got = bytearray()
    seq = 0
    while len(got) < m.declared_length:
        want = min(clen, m.declared_length - len(got))
        req = ctrl(OPS["request"], m.tag, len(got), want, seq)
        assert st.handle_write("chunk_ctrl", req, mtu=mtu).att_error is None
        value = read(st, "chunk_data")
        assert len(value) <= mtu - 1  # fits a single ATT read response
        status, rseq, off, n, payload = chunk(st)
        assert (status, rseq, off, n) == (ST["ok"], seq, len(got), want)
        got += payload
        seq = (seq + 1) & 0xFFFF
    assert hashlib.sha256(got).digest() == m.sha256
    return bytes(got)


@pytest.mark.parametrize("mtu", [23, 185, 247, 517])
def test_full_transfer_after_auth_corrected(mtu):
    st = make(Mode.CORRECTED)
    authorize(st)
    assert _transfer(st, mtu) == st.object_bytes


def test_full_transfer_vulnerable_without_auth():
    st = make(Mode.VULNERABLE)
    assert _transfer(st, 247) == st.object_bytes


def test_chunk_length_rule_incl_mtu_23():
    assert chunk_len_for_mtu(237, 23) == 13
    assert chunk_len_for_mtu(237, 247) == 237
    assert chunk_len_for_mtu(237, 517) == 237
    assert chunk_len_for_mtu(100, 247) == 100
    assert chunk_len_for_mtu(237, 10) == 0
    st = make(Mode.VULNERABLE)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 13, 1), mtu=23)
    assert chunk(st)[0] == ST["ok"]
    assert len(read(st, "chunk_data")) == 22
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 14, 2), mtu=23)
    assert chunk(st)[:2] == (ST["bad_request"], 2)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, st.chunk_max + 1, 3))  # MTU unknown
    assert chunk(st)[0] == ST["bad_request"]
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 0, 4))
    assert chunk(st)[0] == ST["bad_request"]


def test_chunk_max_bounded_by_chunk_data_max_length():
    assert make(Mode.VULNERABLE).chunk_max == PROFILE.characteristics["chunk_data"].max_length - 9
    with pytest.raises(ValueError):
        make(Mode.VULNERABLE, chunk_max=238)
    assert parse_manifest(make(Mode.VULNERABLE, chunk_max=64).manifest).chunk_max == 64


def test_stale_object_tag():
    st = make(Mode.VULNERABLE)
    bad = bytes(b ^ 0xFF for b in st.tag)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], bad, 0, 10, 5))
    assert chunk(st)[:2] == (ST["stale_object"], 5)
    assert diag_status(st) == (ST["stale_object"], 5)


def test_out_of_range_and_tail_clamp():
    st = make(Mode.VULNERABLE)
    size = len(st.object_bytes)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, size, 10, 1))
    assert chunk(st)[0] == ST["out_of_range"]
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, size + 1000, 10, 2))
    assert chunk(st)[0] == ST["out_of_range"]
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, size - 5, 100, 3))
    status, _seq, off, n, payload = chunk(st)
    assert (status, off, n, payload) == (ST["ok"], size - 5, 5, st.object_bytes[-5:])


def test_cancel_and_reset_ops():
    st = make(Mode.VULNERABLE)
    st.handle_write("chunk_ctrl", ctrl(OPS["request"], st.tag, 0, 10, 1))
    st.handle_write("chunk_ctrl", ctrl(OPS["cancel"], st.tag, 0, 0, 2))
    status, seq, _off, n, payload = chunk(st)
    assert (status, seq, n, payload) == (ST["cancelled"], 2, 0, b"")
    assert diag_status(st) == (ST["cancelled"], 2)
    st.handle_write("chunk_ctrl", ctrl(OPS["reset"], st.tag, 0, 0, 3))
    assert chunk(st)[:2] == (ST["no_request"], 0)
    assert diag_status(st) == (ST["no_request"], 0)


def test_unknown_op_and_bad_ctrl_length():
    st = make(Mode.VULNERABLE)
    st.handle_write("chunk_ctrl", ctrl(99, st.tag, 0, 10, 4))
    assert chunk(st)[:2] == (ST["bad_request"], 4)
    assert st.handle_write("chunk_ctrl", b"\x01" * 14).att_error == ATT_INVALID_ATTRIBUTE_VALUE_LENGTH


def test_no_request_initially():
    st = make(Mode.VULNERABLE)
    assert chunk(st)[:2] == (ST["no_request"], 0)
    assert diag_status(st) == (ST["no_request"], 0)


def test_property_enforcement():
    st = make(Mode.VULNERABLE)
    assert st.handle_read("chunk_ctrl").att_error == ATT_READ_NOT_PERMITTED
    assert st.handle_write("diag_manifest", b"\x00").att_error == ATT_WRITE_NOT_PERMITTED
    assert st.handle_write("device_info", b"x").att_error == ATT_WRITE_NOT_PERMITTED


# ----- virtual LED -----
@pytest.mark.parametrize("mode", ALL_MODES)
def test_virtual_led_write_denied_before_auth_in_both_modes(mode):
    st = make(mode)
    assert st.handle_write("virtual_led", b"\x01").att_error == ATT_INSUFFICIENT_AUTHORIZATION
    assert read(st, "virtual_led") == b"\x00"


@pytest.mark.parametrize("mode", ALL_MODES)
def test_virtual_led_write_after_auth_and_restore(mode):
    st = make(mode)
    authorize(st)
    start = read(st, "virtual_led")
    assert st.handle_write("virtual_led", b"\x03").att_error is None
    assert read(st, "virtual_led") == b"\x03"
    assert st.handle_write("virtual_led", start).att_error is None
    assert read(st, "virtual_led") == start
    assert st.handle_write("virtual_led", b"\x09").att_error == ATT_VALUE_NOT_ALLOWED
    assert st.handle_write("virtual_led", b"\x01\x02").att_error == ATT_INVALID_ATTRIBUTE_VALUE_LENGTH


# ----- public / link-level values -----
def test_public_sensor_int16_and_restricted_values():
    st = make(Mode.CORRECTED)
    v = read(st, "public_sensor")
    assert len(v) == 2 and 1900 <= struct.unpack("<h", v)[0] <= 2400
    assert read(st, "restricted_enc") == b"LAB-ENC-OK"
    assert read(st, "restricted_mitm") == b"LAB-MITM-OK"
    for name, spec in PROFILE.characteristics.items():
        if "read" in spec.properties:
            assert len(read(make(Mode.VULNERABLE), name)) <= spec.max_length, name
