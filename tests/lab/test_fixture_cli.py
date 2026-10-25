"""oyster-fixture CLI, reset-state control channel, capability matrix and WinRT import check.

Nothing here advertises, connects or pairs: the WinRT test only builds parameter objects.
"""

import asyncio
import hashlib
import sys
import threading
import time

import pytest
from oyster_lab.fixture import capability
from oyster_lab.fixture.__main__ import EXIT_NO_ACK, EXIT_NOT_READY, EXIT_OK, EXIT_USAGE, main
from oyster_lab.fixture.control import ControlChannel
from oyster_lab.fixture.profile import load_profile
from oyster_lab.fixture.protocol import FixtureState, Mode, auth_response
from oyster_lab.synthetic import make_object, parse_manifest

PROFILE = load_profile()


# ----- control channel -----
def test_reset_request_acknowledged_by_running_fixture(tmp_path):
    fixture_side = ControlChannel(tmp_path)
    fixture_side.prepare()
    resets = []
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("ok", ControlChannel(tmp_path).request_reset(5.0, poll_s=0.01))
    )
    t.start()
    deadline = time.monotonic() + 5.0
    while not resets and time.monotonic() < deadline:
        fixture_side.poll_reset(lambda: resets.append(1))
        time.sleep(0.01)
    t.join(6.0)
    assert resets == [1]
    assert result["ok"] is True
    assert not fixture_side.request_path.exists()
    assert not fixture_side.ack_path.exists()


def test_reset_request_without_fixture_is_withdrawn(tmp_path):
    ch = ControlChannel(tmp_path)
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s

    assert ch.request_reset(0.5, poll_s=0.1, sleep=sleep, clock=clock) is False
    assert not ch.request_path.exists()


def test_malformed_reset_request_ignored(tmp_path):
    ch = ControlChannel(tmp_path)
    ch.prepare()
    ch.request_path.write_text("../../etc", encoding="ascii")
    called = []
    assert ch.poll_reset(lambda: called.append(1)) is False
    assert called == [] and not ch.request_path.exists()


def test_cli_reset_state_without_fixture(tmp_path, capsys):
    assert main(["reset-state", "--control-dir", str(tmp_path), "--timeout", "0.2"]) == EXIT_NO_ACK
    assert "no running fixture acknowledged" in capsys.readouterr().err


# ----- run refuses without a proper token (before any Bluetooth call) -----
@pytest.mark.parametrize("value", [None, "", "short-token"])
def test_run_refuses_without_32_byte_token(monkeypatch, capsys, value):
    monkeypatch.setattr(capability, "run_check", _fail_if_called)
    if value is None:
        monkeypatch.delenv("LAB_FIXTURE_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LAB_FIXTURE_AUTH_TOKEN", value)
    assert main(["run", "--mode", "corrected"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "refusing to start" in err
    if value:
        assert value not in err


def test_run_honours_token_env_name(monkeypatch, capsys):
    monkeypatch.setattr(capability, "run_check", _fail_if_called)
    monkeypatch.setenv("LAB_FIXTURE_AUTH_TOKEN", "x" * 40)
    monkeypatch.delenv("OTHER_TOKEN_VAR", raising=False)
    assert main(["run", "--mode", "vulnerable", "--token-env", "OTHER_TOKEN_VAR"]) == EXIT_USAGE
    assert "OTHER_TOKEN_VAR" in capsys.readouterr().err


def _fail_if_called(*_a, **_k):
    raise AssertionError("Bluetooth must not be touched before the token check")


def test_run_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        main(["run", "--mode", "half-open"])


# ----- synth -----
def test_synth_writes_object_manifest_and_sha256(tmp_path, capsys):
    assert main(["synth", "--out", str(tmp_path)]) == EXIT_OK
    oid = PROFILE.object_id
    obj = (tmp_path / f"{oid}.bin").read_bytes()
    manifest = parse_manifest((tmp_path / f"{oid}.manifest.bin").read_bytes())
    digest = hashlib.sha256(obj).hexdigest()
    assert obj == make_object(oid)
    assert manifest.sha256.hex() == digest
    assert manifest.declared_length == len(obj)
    assert manifest.chunk_max == 237
    assert (tmp_path / f"{oid}.sha256").read_bytes() == f"{digest}  {oid}.bin\n".encode()  # LF only
    assert digest in capsys.readouterr().out


def test_synth_rejects_bad_object_id_and_chunk_max(tmp_path):
    assert main(["synth", "--out", str(tmp_path), "--object-id", "../evil"]) == EXIT_USAGE
    assert main(["synth", "--out", str(tmp_path), "--chunk-max", "238"]) == EXIT_USAGE
    assert list(tmp_path.iterdir()) == []


# ----- capability matrix (pure) -----
READY = capability.AdapterInfo(
    present=True, low_energy=True, peripheral_role=True, central_role=True, le_secure_connections=True,
    extended_advertising=False, max_advertisement_data_length=31, radio_state="on",
)
API = capability.ApiInfo(True, ("PLAIN", "ENCRYPTION_REQUIRED", "ENCRYPTION_AND_AUTHENTICATION_REQUIRED"))


def test_capability_matrix_ready_host():
    rows = {r.capability: r for r in capability.build_matrix(READY, API)}
    assert capability.is_ready(READY, API)
    assert rows["BLE peripheral role"].status == "supported"
    assert rows["Link encryption policy (restricted_enc)"].status == "supported"
    assert rows["Authenticated pairing policy (restricted_mitm)"].status == "unverified"
    assert "Cardputer unverified" in rows["Authenticated pairing policy (restricted_mitm)"].detail
    assert rows["Application authorization (app_auth)"].status == "supported"
    assert rows["Per-connection authorization state"].status == "unsupported"
    text = capability.format_matrix(list(rows.values()))
    assert "BLE peripheral role" in text and "unverified" in text


@pytest.mark.parametrize(
    "adapter",
    [
        capability.AdapterInfo(present=False, error="no default Bluetooth adapter"),
        capability.AdapterInfo(present=True, low_energy=True, peripheral_role=False, radio_state="on"),
        capability.AdapterInfo(present=True, low_energy=True, peripheral_role=True, radio_state="off"),
    ],
)
def test_capability_not_ready(adapter):
    assert not capability.is_ready(adapter, API)
    assert capability.not_ready_reasons(adapter, API)
    capability.build_matrix(adapter, API)  # renders without raising


def test_radio_off_reason_tells_owner_what_to_do():
    off = capability.AdapterInfo(present=True, low_energy=True, peripheral_role=True, radio_state="off")
    assert any("turn Bluetooth on" in r for r in capability.not_ready_reasons(off, API))


@pytest.mark.skipif(sys.platform != "win32", reason="run is Windows-only")
def test_run_stops_before_publishing_when_host_not_ready(monkeypatch, capsys, tmp_path):
    off = capability.AdapterInfo(present=True, low_energy=True, peripheral_role=True, radio_state="off")

    async def fake_check():
        return off, API, capability.build_matrix(off, API)

    monkeypatch.setattr(capability, "run_check", fake_check)
    monkeypatch.setenv("LAB_FIXTURE_AUTH_TOKEN", "k" * 40)
    rc = main(["run", "--mode", "corrected", "--control-dir", str(tmp_path)])
    assert rc == EXIT_NOT_READY
    err = capsys.readouterr().err
    assert "radio is off" in err and "k" * 40 not in err


# ----- WinRT binding: import check only -----
@pytest.mark.skipif(sys.platform != "win32", reason="WinRT is Windows-only")
def test_winrt_binding_imports_and_maps_profile():
    g = pytest.importorskip("winrt.windows.devices.bluetooth.genericattributeprofile")
    gw = pytest.importorskip("oyster_lab.fixture.gatt_winrt")
    p = g.GattCharacteristicProperties
    lvl = g.GattProtectionLevel
    assert gw.properties_flags(("read", "notify")) == p.READ | p.NOTIFY
    expected_read = {
        "restricted_enc": lvl.ENCRYPTION_REQUIRED,
        "restricted_mitm": lvl.ENCRYPTION_AND_AUTHENTICATION_REQUIRED,
    }
    for name, spec in PROFILE.characteristics.items():
        params = gw.characteristic_parameters(spec)
        assert params.read_protection_level == expected_read.get(name, lvl.PLAIN), name
        assert params.write_protection_level == lvl.PLAIN, name
        assert bool(params.characteristic_properties & p.WRITE) == ("write" in spec.properties), name
    assert bytes(gw.to_buffer(b"\x01\x02\x03")) == b"\x01\x02\x03"
    assert bytes(gw.to_buffer(b"")) == b""
    assert hasattr(gw.WinRTFixtureServer, "start") and hasattr(gw.WinRTFixtureServer, "stop")


# ----- WinRT request bridge driven by fake request objects (no Bluetooth) -----
class _FakeDeferral:
    def __init__(self):
        self.completed = asyncio.Event()

    def complete(self):
        self.completed.set()


class _FakeRequest:
    def __init__(self, offset=0, value=None, option=None):
        self.offset = offset
        self.value = value
        self.option = option
        self.responses = []

    def respond_with_value(self, buf):
        self.responses.append(("value", bytes(buf)))

    def respond(self):
        self.responses.append(("ok", None))

    def respond_with_protocol_error(self, code):
        self.responses.append(("error", code))


class _FakeSession:
    max_pdu_size = 247


class _FakeArgs:
    def __init__(self, request):
        self._request = request
        self.deferral = _FakeDeferral()
        self.session = _FakeSession()

    def get_deferral(self):
        return self.deferral

    async def get_request_async(self):
        return self._request


@pytest.mark.skipif(sys.platform != "win32", reason="WinRT is Windows-only")
async def test_winrt_bridge_serves_requests_from_winrt_threads():
    gw = pytest.importorskip("oyster_lab.fixture.gatt_winrt")
    from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption

    token = b"bridge-test-token-0123456789abcdef"
    state = FixtureState(PROFILE, Mode.CORRECTED, token)
    server = gw.WinRTFixtureServer(PROFILE, state)
    server._loop = asyncio.get_running_loop()

    async def read(name, offset=0):
        req = _FakeRequest(offset=offset)
        args = _FakeArgs(req)
        # WinRT raises the event on its own thread
        await asyncio.to_thread(server._make_read_handler(name), None, args)
        await asyncio.wait_for(args.deferral.completed.wait(), 2)
        return req.responses

    async def write(name, data):
        req = _FakeRequest(value=data, option=GattWriteOption.WRITE_WITH_RESPONSE)
        args = _FakeArgs(req)
        await asyncio.to_thread(server._make_write_handler(name), None, args)
        await asyncio.wait_for(args.deferral.completed.wait(), 2)
        return req.responses

    assert await read("diag_manifest") == [("value", b"\x01")]
    assert await write("virtual_led", b"\x02") == [("error", 0x08)]
    [(kind, value)] = await read("app_auth")
    assert kind == "value" and value[0] == 0
    assert await write("app_auth", auth_response(token, value[1:])) == [("ok", None)]
    [(kind, manifest)] = await read("diag_manifest")
    assert kind == "value" and manifest == state.manifest
    assert await read("diag_manifest", offset=50) == [("value", state.manifest[50:])]
    assert await write("virtual_led", b"\x09") == [("error", 0x13)]
    # request withdrawn by Windows: deferral still completed, nothing sent
    args = _FakeArgs(None)
    await asyncio.to_thread(server._make_read_handler("device_info"), None, args)
    await asyncio.wait_for(args.deferral.completed.wait(), 2)
