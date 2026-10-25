"""oyster-fixture: live lab BLE GATT fixture on this Windows PC (synthetic data only, ADR-007/ADR-008).

  oyster-fixture check                         read-only adapter capability matrix
  oyster-fixture run --mode vulnerable|corrected [--token-env LAB_FIXTURE_AUTH_TOKEN]
  oyster-fixture reset-state                   clear authorization/transfer state of a running fixture
  oyster-fixture synth --out DIR               write the synthetic object, manifest and sha256

`run` advertises the lab service until Ctrl+C. The mode is shown locally and never exposed over GATT.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from oyster_lab.fixture import capability
from oyster_lab.fixture.control import CONTROL_POLL_S, ControlChannel
from oyster_lab.fixture.profile import LabProfile, load_profile
from oyster_lab.fixture.protocol import MIN_TOKEN_BYTES, FixtureState, Mode
from oyster_lab.synthetic import is_valid_object_id, make_object, manifest_bytes, object_tag, sha256_hex

log = logging.getLogger("oyster_lab.fixture")

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_USAGE = 2
EXIT_NO_ACK = 3


def _err(msg: str) -> None:
    print(f"oyster-fixture: {msg}", file=sys.stderr)


def _load_profile(path: Path | None) -> LabProfile | None:
    try:
        return load_profile(path)
    except (OSError, ValueError) as exc:
        _err(f"cannot load lab profile: {exc}")
        return None


def read_token(env_name: str) -> bytes | None:
    """Token bytes from the environment, or None when missing/too short. Never printed."""
    raw = os.environ.get(env_name, "")
    key = raw.encode("utf-8")
    return key if len(key) >= MIN_TOKEN_BYTES else None


# ----- check -----
def cmd_check(args: argparse.Namespace) -> int:
    adapter, api, rows = asyncio.run(capability.run_check())
    ready = capability.is_ready(adapter, api)
    if args.json:
        print(capability.matrix_json(adapter, api, rows))
    else:
        print("Oyster lab fixture - host capability check (read-only; nothing advertised or paired)")
        print(capability.format_matrix(rows))
        print()
        print("ready to run the fixture" if ready else "NOT ready: the fixture cannot run on this host")
        for reason in capability.not_ready_reasons(adapter, api):
            print(f"  - {reason}")
    return EXIT_OK if ready else EXIT_NOT_READY


# ----- run -----
async def _serve(profile: LabProfile, state: FixtureState, control: ControlChannel, notify_s: float) -> None:
    from oyster_lab.fixture.gatt_winrt import WinRTFixtureServer  # Windows only

    server = WinRTFixtureServer(profile, state)
    loop = asyncio.get_running_loop()
    try:
        await server.start()
        log.info("advertising service %s (connectable, discoverable); Ctrl+C to stop", profile.service_uuid)
        next_notify = loop.time() + notify_s
        while True:
            await asyncio.sleep(CONTROL_POLL_S)
            state.expire()
            if control.poll_reset(state.reset):
                log.info("reset-state request handled")
            if notify_s > 0 and loop.time() >= next_notify:
                next_notify = loop.time() + notify_s
                try:
                    await server.notify_sensor()
                except OSError as exc:
                    log.warning("public_sensor notify failed: %s", exc)
    finally:
        server.stop()


def cmd_run(args: argparse.Namespace) -> int:
    token = read_token(args.token_env)
    if token is None:
        _err(f"refusing to start: environment variable {args.token_env} must hold a token of at least "
             f"{MIN_TOKEN_BYTES} bytes (the same enrolled test credential the Cardputer uses)")
        return EXIT_USAGE
    profile = _load_profile(args.profile)
    if profile is None:
        return EXIT_USAGE
    if sys.platform != "win32":
        _err("unsupported: the live fixture needs the Windows WinRT GATT server (ADR-008); use the simulator")
        return EXIT_USAGE
    adapter, api, _rows = asyncio.run(capability.run_check())
    reasons = capability.not_ready_reasons(adapter, api)
    if reasons:
        _err("Bluetooth host not ready: " + "; ".join(reasons) + " (see `oyster-fixture check`)")
        return EXIT_NOT_READY
    state = FixtureState(profile, Mode(args.mode), token)
    del token
    control = ControlChannel(args.control_dir)
    control.prepare()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Oyster lab fixture (synthetic data only) - profile %s v%d",
             profile.profile_id, profile.profile_version)
    log.info("mode: %s (local only; never exposed over GATT)", state.mode.value)
    log.info("object %s: %d bytes, sha256 %s, chunk_max %d", profile.object_id, len(state.object_bytes),
             sha256_hex(state.object_bytes), state.chunk_max)
    log.info("reset-state control directory: %s", control.directory)
    try:
        asyncio.run(_serve(profile, state, control, args.notify_interval))
    except KeyboardInterrupt:
        log.info("stopped by Ctrl+C")
    except Exception as exc:  # FixtureStartError, WinRT OSError
        log.error("fixture stopped: %s", exc)
        return EXIT_NOT_READY
    return EXIT_OK


# ----- reset-state -----
def cmd_reset_state(args: argparse.Namespace) -> int:
    control = ControlChannel(args.control_dir)
    if control.request_reset(args.timeout):
        print("fixture state reset: authorization, challenge, transfer and virtual LED cleared")
        return EXIT_OK
    _err(f"no running fixture acknowledged within {args.timeout:g} s (control dir {control.directory}); "
         "nothing to reset - a fresh `oyster-fixture run` always starts unauthorized")
    return EXIT_NO_ACK


# ----- synth -----
def cmd_synth(args: argparse.Namespace) -> int:
    profile = _load_profile(args.profile)
    if profile is None:
        return EXIT_USAGE
    object_id = args.object_id or profile.object_id
    if not is_valid_object_id(object_id):
        _err("object id must be 1..16 characters of [A-Za-z0-9_-]")
        return EXIT_USAGE
    limit = profile.characteristics["chunk_data"].max_length - profile.chunk_header_bytes
    chunk_max = args.chunk_max or limit
    if not 1 <= chunk_max <= limit:
        _err(f"--chunk-max must be 1..{limit}")
        return EXIT_USAGE
    obj = make_object(object_id)
    manifest = manifest_bytes(obj, object_id, chunk_max)
    digest = sha256_hex(obj)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{object_id}.bin").write_bytes(obj)
    (out / f"{object_id}.manifest.bin").write_bytes(manifest)
    (out / f"{object_id}.sha256").write_bytes(f"{digest}  {object_id}.bin\n".encode("ascii"))  # sha256sum -c
    print(f"object_id       {object_id} (synthetic)")
    print(f"declared_length {len(obj)}")
    print(f"sha256          {digest}")
    print(f"object_tag      {object_tag(obj).hex()}")
    print(f"chunk_max       {chunk_max}")
    print(f"written to      {out.resolve()}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oyster-fixture", description="Oyster lab BLE GATT fixture (synthetic data)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="read-only Bluetooth adapter capability matrix")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="publish and advertise the lab GATT service until Ctrl+C")
    p.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    p.add_argument("--token-env", default="LAB_FIXTURE_AUTH_TOKEN", help="env var holding the auth token")
    p.add_argument("--profile", type=Path, default=None, help="default: schemas/lab-profile.json")
    p.add_argument("--control-dir", type=Path, default=None, help="reset-state control directory")
    p.add_argument("--notify-interval", type=float, default=5.0,
                   help="public_sensor notify period in s (0 = off)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("reset-state", help="clear authorization/transfer state of the running fixture")
    p.add_argument("--control-dir", type=Path, default=None)
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_reset_state)

    p = sub.add_parser("synth", help="write the synthetic object, manifest and sha256 for verification")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--object-id", default=None)
    p.add_argument("--chunk-max", type=int, default=None)
    p.add_argument("--profile", type=Path, default=None)
    p.set_defaults(func=cmd_synth)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
