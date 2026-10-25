"""`oyster` command line.

Owner commands that have an admin route (enroll-device, enroll-target, list-targets, export-report) call the
local HTTP listener (http://127.0.0.1:OYSTER_HTTP_PORT) with OYSTER_ADMIN_TOKEN; `--direct` writes/reads the
SQLite database instead (works without a running server). Commands without a route (init-db, sessions,
link-baseline) always use the database.

Extension hook: oyster.ai.cli may define `register(subparsers)` taking the argparse subparsers object; each
parser it adds must `set_defaults(func=handler)` with `handler(args) -> int`. It provides `ai-check`
and `ask`; when it is missing those names report the AI package as unavailable.

`serve` runs two apps behind two uvicorn servers in one process and one event loop: HTTPS on
OYSTER_BIND_HOST:OYSTER_HTTPS_PORT with the dev certificate (when present) for the Cardputer, serving ONLY the
device routes and never the owner token (audience "device"), and plain HTTP on 127.0.0.1:OYSTER_HTTP_PORT
for the CLI and simulator, serving every route (audience "loopback"). uvicorn's per-server signal capture
is disabled and one SIGINT/SIGTERM handler stops both; the run worker and expiry sweep are one shared
Runtime, started once for both listeners.

Owner commands never send the admin token to a non-loopback host unless --allow-remote-admin is given.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from oyster import auth
from oyster.fileperms import write_private_file
from oyster.settings import Settings, load_settings

try:  # the AI commands register themselves through this hook
    from oyster.ai.cli import register as _ai_register
except ImportError:
    _ai_register = None

AI_COMMANDS = {
    "ai-check": "Check the configured AI provider (needs the oyster.ai package)",
    "ask": "Ask a question about a session (needs the oyster.ai package)",
}
ADMIN_TOKEN_PLACEHOLDER = auth.ADMIN_TOKEN_PLACEHOLDER
# The device polls every 4 s over one persistent TLS connection; uvicorn's 5 s default keep-alive would
# close it between polls and force a new TLS handshake each time.
KEEP_ALIVE_S = 30


@dataclass
class Command:
    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    handler: Callable[[argparse.Namespace], int]


REGISTRY: dict[str, Command] = {}


def command(name: str, help: str, configure: Callable[[argparse.ArgumentParser], None] | None = None):
    def deco(fn: Callable[[argparse.Namespace], int]):
        REGISTRY[name] = Command(name, help, configure or (lambda _p: None), fn)
        return fn

    return deco


class CliError(RuntimeError):
    """User-facing failure: printed without a traceback, exit status 2."""


# --- helpers ---
def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(getattr(args, "env_file", None))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _store(settings: Settings):
    from oyster.db.sqlite_store import SqliteStore

    return SqliteStore(settings.db_path)


def check_admin_destination(base_url: str, allow_remote: bool = False) -> None:
    """Refuse to send the admin token to a non-loopback host unless the owner passed --allow-remote-admin."""
    problem = auth.admin_destination_problem(base_url, allow_remote)
    if problem:
        raise CliError(problem)
    if allow_remote and auth.admin_destination_problem(base_url, False):
        scheme = base_url.split(":", 1)[0].lower()
        _err(
            f"warning: sending the admin token to {base_url}"
            + (" over plain http (clear text)" if scheme == "http" else "")
        )


def owner_client(
    settings: Settings, base_url: str | None = None, *, allow_remote: bool = False
) -> httpx.Client:
    """HTTP client for owner routes on the local listener (tests replace this with a TestClient)."""
    if not settings.admin_token:
        raise CliError(
            "OYSTER_ADMIN_TOKEN is not set (backend/.env); owner commands need it, or use --direct"
        )
    url = base_url or f"http://127.0.0.1:{settings.http_port}"
    problem = auth.admin_destination_problem(url, allow_remote)
    if problem:
        raise CliError(problem)
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
        timeout=20.0,
    )


def _call(settings: Settings, args: argparse.Namespace, method: str, path: str, **kw) -> httpx.Response:
    backend = getattr(args, "backend", None)
    allow = bool(getattr(args, "allow_remote_admin", False))
    if backend:  # checked before any client is built, so a monkeypatched client cannot skip the rule
        check_admin_destination(backend, allow)
    try:
        with owner_client(settings, backend, **({"allow_remote": True} if allow else {})) as c:
            r = c.request(method, path, **kw)
    except httpx.TransportError as e:
        raise CliError(
            f"backend not reachable ({type(e).__name__}); start `oyster serve` or use --direct"
        ) from None
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = f"{body.get('error')}: {body.get('detail')}"
        except ValueError:
            msg = r.text[:200]
        raise CliError(f"{method} {path} -> HTTP {r.status_code} {msg}")
    return r


def _owner_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--direct", action="store_true", help="use the SQLite database instead of the HTTP API")
    p.add_argument("--backend", help="owner API base URL (default http://127.0.0.1:OYSTER_HTTP_PORT)")
    p.add_argument(
        "--allow-remote-admin",
        action="store_true",
        help="allow --backend to name a non-loopback host (the admin token is then sent there)",
    )


def _write_secret_file(path: Path, text: str) -> str | None:
    """Write a secret file with owner-only access; returns a warning when that could not be confirmed."""
    return write_private_file(path, (text + "\n").encode("utf-8"))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- commands ---
@command("init-db", "Create the data directory and the SQLite schema")
def cmd_init_db(args: argparse.Namespace) -> int:
    s = _settings(args)
    store = _store(s)
    store.close()
    print(f"database ready: {s.db_path}")
    return 0


def _conf_serve(p: argparse.ArgumentParser) -> None:
    p.add_argument("--http-only", action="store_true", help="only the 127.0.0.1 HTTP listener")
    p.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])


def build_worker(store, settings: Settings):
    """RunWorker from oyster.ai.runner, or None when the AI package is missing (runs end ai_unavailable)."""
    try:
        from oyster.ai.runner import RunWorker, build_provider
    except ImportError as e:
        _err(
            f"AI package unavailable ({e.name or 'oyster.ai.runner'}): assistant runs will end ai_unavailable"
        )
        return None
    provider = build_provider(settings)
    if provider is None:
        _err(
            "AI provider not configured (AI_PROVIDER/AI_MODEL/AI_API_KEY): "
            "assistant runs will end ai_unavailable"
        )
    return RunWorker(store, settings, provider)


@command("serve", "Run the backend: HTTPS for the device + HTTP on 127.0.0.1 for local tools", _conf_serve)
def cmd_serve(args: argparse.Namespace) -> int:
    from oyster.main import create_app

    s = _settings(args)
    problem = auth.admin_token_problem(s.admin_token)
    if problem:
        raise CliError(
            f"OYSTER_ADMIN_TOKEN {problem}. Set it in backend/.env to a random value of at least "
            f"{auth.MIN_ADMIN_TOKEN_LEN} characters, for example the output of: {auth.ADMIN_TOKEN_COMMAND}"
        )
    store = _store(s)
    worker = build_worker(store, s)
    app = create_app(s, store=store, worker=worker)
    configs = server_configs(app, s, http_only=args.http_only, log_level=args.log_level)
    run_servers(configs, app.state.runtime, on_exit=_stop_claude_waits(app))
    store.close()
    return 0


def _stop_claude_waits(app) -> Callable[[], None]:
    """Ends the Claude Code approvals that are still waiting so a Ctrl+C does not wait for their long poll."""

    def stop() -> None:
        runtime = getattr(app.state, "claude", None)
        if runtime is not None:
            runtime.stop()

    return stop


def server_configs(app, s: Settings, *, http_only: bool = False, log_level: str = "info") -> list:
    """uvicorn configs: `app` (the loopback app: every route) on 127.0.0.1 for local tools, plus a separate
    device-audience app (device routes only, owner token refused) on bind_host over HTTPS when a certificate
    exists. Both keep idle connections open for KEEP_ALIVE_S so the device's polls reuse one TLS session."""
    import uvicorn

    from oyster.main import create_app

    if getattr(app.state, "audience", None) != "loopback":
        raise ValueError("server_configs needs the loopback app; the device app is derived from it")

    common = {
        "lifespan": "off",
        "log_level": log_level,
        "server_header": False,
        "timeout_keep_alive": KEEP_ALIVE_S,
    }
    configs = [uvicorn.Config(app, host="127.0.0.1", port=s.http_port, **common)]
    if http_only:
        _err("HTTPS listener disabled (--http-only)")
    elif s.tls_cert.is_file() and s.tls_key.is_file():
        device_app = create_app(
            s,
            store=app.state.store,
            worker=app.state.worker,
            audience="device",
            runtime=app.state.runtime,
        )
        configs.append(
            uvicorn.Config(
                device_app,
                host=s.bind_host,
                port=s.https_port,
                ssl_certfile=str(s.tls_cert),
                ssl_keyfile=str(s.tls_key),
                **common,
            )
        )
    else:
        _err(
            f"HTTPS listener disabled: no certificate at {s.tls_cert} "
            "(run `oyster make-dev-ca --ip <LAN-IP>`)"
        )
    return configs


def run_servers(configs: list, runtime, on_exit: Callable[[], None] | None = None) -> None:
    """Serve several uvicorn configs in one event loop with one shared shutdown signal. `on_exit` runs when
    the signal arrives, before the listeners are asked to stop."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # one handler below stops every listener
            yield

    servers = [_Server(c) for c in configs]

    def _exit(sig, frame):
        if on_exit is not None:
            on_exit()
        for srv in servers:
            srv.handle_exit(sig, frame)

    sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    previous = {sig: signal.signal(sig, _exit) for sig in sigs}

    async def _all() -> None:
        await asyncio.gather(*(srv.serve() for srv in servers))

    runtime.start()
    try:
        asyncio.run(_all())
    finally:
        runtime.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _conf_seed_demo(p: argparse.ArgumentParser) -> None:
    p.add_argument("--id", required=True, help="device id that will see the demo host and the Desk profile")
    p.add_argument("--host-id", default="this-pc", help="id of the local PC host (default this-pc)")
    p.add_argument("--services", default="", help="comma-separated Windows services to report (allowlist)")
    p.add_argument("--no-profile", action="store_true", help="only enrol the host, not the Desk profile")
    _owner_args(p)


@command(
    "seed-demo",
    "Enrol this PC as a read-only host and create the Desk profile for a device (backend must be running)",
    _conf_seed_demo,
)
def cmd_seed_demo(args: argparse.Namespace) -> int:
    from oyster.deck.seed import desk_profile_dict

    s = _settings(args)
    services = [x.strip() for x in args.services.split(",") if x.strip()]
    host = {
        "host_id": args.host_id,
        "display_name": "This PC",
        "adapter": "local",
        "metrics": ["cpu_percent", "mem_used_percent", "disk_used_percent", "uptime_s"],
        "services": services,
        "device_ids": [args.id],
    }
    r = _call(s, args, "POST", "/v1/admin/hosts", json=host)
    state = "created" if r.json().get("created") else "updated"
    print(f"host {args.host_id}: {state} (local adapter, read-only)")
    if not args.no_profile:
        profile = desk_profile_dict([args.id], args.host_id)
        r = _call(s, args, "PUT", "/v1/deck/profiles/desk", json=profile)
        print(f"profile desk: {r.json().get('result', 'ok')} (slots 1-9 act on host {args.host_id})")
    return 0


def _conf_ca(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--ip", required=True, help="the backend PC's LAN IP (CN and SAN of the server certificate)"
    )
    p.add_argument("--out", help="output directory (default <data>/dev-ca)")
    p.add_argument(
        "--new-ca", action="store_true", help="replace the CA (provisioned devices must be re-provisioned)"
    )


@command("make-dev-ca", "Create/refresh the private dev CA and a server certificate for --ip", _conf_ca)
def cmd_make_dev_ca(args: argparse.Namespace) -> int:
    from oyster.tls import make_dev_ca

    s = _settings(args)
    try:
        res = make_dev_ca(args.ip, Path(args.out) if args.out else s.data_dir / "dev-ca", new_ca=args.new_ca)
    except ValueError as e:
        raise CliError(f"--ip: {e}") from None
    print(f"CA {'created' if res.ca_created else 'reused'}: {res.ca_cert}  (SHA-256 {res.ca_sha256})")
    print(f"server certificate for {res.ip}: {res.server_cert}  (SHA-256 {res.server_sha256})")
    if res.warnings:
        print(
            f"private keys written to {res.ca_key.parent} "
            "(never printed; keep them off the device and out of git)"
        )
        for w in res.warnings:
            _err(f"warning: owner-only protection NOT confirmed: {w}")
    else:
        print(
            f"private keys written to {res.ca_key.parent} with owner-only access "
            "(never printed; keep them off the device and out of git)"
        )
    return 0


def _conf_enroll_device(p: argparse.ArgumentParser) -> None:
    p.add_argument("--id", required=True, help="device id, [0-9a-zA-Z_-]{1,40}")
    p.add_argument("--name", help="display name (default: the id)")
    p.add_argument(
        "--simulated", action="store_true", help="simulator identity: may only open simulated sessions"
    )
    p.add_argument("--write-token", metavar="FILE", help="write the token to FILE instead of printing it")
    _owner_args(p)


def enroll_device(
    settings: Settings, args: argparse.Namespace, device_id: str, name: str | None, simulated: bool
) -> str:
    """Enrol (or rotate) a device token through the admin API or, with --direct, the database."""
    if getattr(args, "direct", False):
        import re

        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,40}", device_id):
            raise CliError("device id must match [0-9a-zA-Z_-]{1,40}")
        store = _store(settings)
        try:
            return store.create_device(device_id, name or device_id, simulated)
        finally:
            store.close()
    r = _call(
        settings,
        args,
        "POST",
        "/v1/admin/devices",
        json={"device_id": device_id, "display_name": name or device_id, "simulated": simulated},
    )
    return r.json()["token"]


@command(
    "enroll-device",
    "Enrol a device (or rotate its token); the token is shown exactly once",
    _conf_enroll_device,
)
def cmd_enroll_device(args: argparse.Namespace) -> int:
    s = _settings(args)
    token = enroll_device(s, args, args.id, args.name, args.simulated)
    if args.write_token:
        warning = _write_secret_file(Path(args.write_token), token)
        _err(
            f"device {args.id} enrolled; token written to {args.write_token} (any previous token is revoked)"
        )
        if warning:
            _err(f"warning: owner-only protection NOT confirmed: {warning}")
    else:
        _err(f"device {args.id} enrolled; token follows (shown once; any previous token is revoked):")
        print(token)
    return 0


def _conf_enroll_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("--file", required=True, help="TargetProfile JSON (schemas/target-profile.schema.json)")
    _owner_args(p)


@command("enroll-target", "Enrol or update a target profile", _conf_enroll_target)
def cmd_enroll_target(args: argparse.Namespace) -> int:
    s = _settings(args)
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read {args.file}: {e}") from None
    if args.direct:
        from oyster.evidence.validate import SchemaValidationError, parse_target
        from oyster.policy import catalogue

        try:
            target = parse_target(data)
        except SchemaValidationError as e:
            raise CliError(str(e)) from None
        if catalogue.load_profile(target.profile_id) is None:
            raise CliError(f"unknown profile_id {target.profile_id!r}")
        store = _store(s)
        try:
            from oyster.store import Conflict

            try:
                store.upsert_target(target)
            except Conflict as e:
                raise CliError(f"{e.code}: {e.detail}") from None
        finally:
            store.close()
    else:
        _call(s, args, "POST", "/v1/admin/targets", json=data)
    print(f"target {data.get('target_id')} enrolled (scope_version {data.get('scope_version')})")
    return 0


@command("list-targets", "List enrolled targets", _owner_args)
def cmd_list_targets(args: argparse.Namespace) -> int:
    s = _settings(args)
    if args.direct:
        store = _store(s)
        targets = [t.model_dump(mode="json") for t in store.list_targets()]
        store.close()
    else:
        targets = _call(s, args, "GET", "/v1/admin/targets").json()["targets"]
    if not targets:
        print("no targets enrolled")
    for t in targets:
        print(
            f"{t['target_id']}  profile={t['profile_id']}  scope_version={t['scope_version']}  "
            f"ops={','.join(t['allowed_operations'])}"
        )
    return 0


def _conf_sessions(p: argparse.ArgumentParser) -> None:
    p.add_argument("--device", help="only sessions of this device")


@command("sessions", "List sessions (reads the database)", _conf_sessions)
def cmd_sessions(args: argparse.Namespace) -> int:
    store = _store(_settings(args))
    try:
        rows = store.list_sessions(args.device)
        if not rows:
            print("no sessions")
        for r in rows:
            base = f"  baseline={r.linked_baseline_session_id}" if r.linked_baseline_session_id else ""
            print(
                f"{r.session_id}  device={r.device_id}  mode={r.source_mode}  created={_iso(r.created_at)}  "
                f"events={store.count_events(r.session_id)}{base}"
            )
    finally:
        store.close()
    return 0


def _conf_link(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", required=True)
    p.add_argument("--baseline", help="baseline session id; omit to clear the link")


@command(
    "link-baseline", "Link a baseline session that reports may cite (reads/writes the database)", _conf_link
)
def cmd_link_baseline(args: argparse.Namespace) -> int:
    from oyster.store import Conflict, NotFound

    store = _store(_settings(args))
    try:
        store.link_baseline(args.session, args.baseline)
    except NotFound as e:
        raise CliError(f"unknown session {e}") from None
    except Conflict as e:
        raise CliError(f"{e.code}: {e.detail}") from None
    finally:
        store.close()
    print(f"{args.session}: baseline {'= ' + args.baseline if args.baseline else 'cleared'}")
    return 0


def _conf_report(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", required=True)
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.add_argument("--out", help="write to this file instead of stdout")
    _owner_args(p)


@command("export-report", "Export a session report (Markdown or JSON)", _conf_report)
def cmd_export_report(args: argparse.Namespace) -> int:
    s = _settings(args)
    if args.direct:
        from oyster.reports.render import build_report, render_markdown
        from oyster.store import NotFound

        store = _store(s)
        try:
            rep = build_report(store, args.session)
        except NotFound:
            raise CliError(f"unknown session {args.session}") from None
        finally:
            store.close()
        text = render_markdown(rep) if args.format == "md" else json.dumps(rep, indent=2)
    else:
        r = _call(s, args, "GET", f"/v1/sessions/{args.session}/report", params={"format": args.format})
        text = r.text if args.format == "md" else json.dumps(r.json(), indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _err(f"report written to {args.out}")
    else:
        print(text)
    return 0


@command("validate-schemas", "Validate schemas/examples and the lab profile against the JSON schemas")
def cmd_validate_schemas(args: argparse.Namespace) -> int:
    from oyster.evidence.validate import validate_all

    failures = 0
    for name, problems in validate_all().items():
        print(f"{'OK  ' if not problems else 'FAIL'} {name}")
        for p in problems:
            print(f"     {p}")
        failures += bool(problems)
    print(f"{failures} failure(s)")
    return 1 if failures else 0


def _conf_provision(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", required=True, help="serial port, e.g. COM5")
    p.add_argument("--id", required=True, help="device id to set and enrol")
    p.add_argument("--name", help="display name for enrolment")
    p.add_argument("--backend-host", help="LAN IP the server certificate names (default: from server.crt)")
    p.add_argument("--backend-port", type=int, help="HTTPS port (default OYSTER_HTTPS_PORT)")
    p.add_argument(
        "--ssid", help="2.4 GHz Wi-Fi SSID (prompted when omitted; the password is always prompted)"
    )
    p.add_argument("--no-wifi", action="store_true", help="leave the Wi-Fi settings unchanged")
    p.add_argument("--ca", help="CA certificate PEM (default <data>/dev-ca/ca.crt)")
    p.add_argument("--token-file", help="use this existing token instead of enrolling (no rotation)")
    p.add_argument(
        "--target-file",
        action="append",
        default=[],
        help="TargetProfile JSON to enrol locally "
        "(repeatable; default: every target enrolled on the backend)",
    )
    p.add_argument("--no-targets", action="store_true", help="do not change the device's target list")
    p.add_argument(
        "--allow-long-lines",
        action="store_true",
        help="send TARGET-ADD lines over the documented 512-byte limit (firmware must accept them)",
    )
    _owner_args(p)


@command("provision-device", "Provision a Cardputer over USB serial (docs/PROVISIONING.md)", _conf_provision)
def cmd_provision_device(args: argparse.Namespace) -> int:
    from oyster.provision import MAX_LINE, ProvisioningError, ProvisionPlan, SerialLink, provision
    from oyster.tls import server_cert_ip

    s = _settings(args)
    host = args.backend_host or server_cert_ip(s.tls_cert)
    if not host:
        raise CliError("no --backend-host and no server certificate to read it from (run make-dev-ca first)")
    ca_path = Path(args.ca) if args.ca else s.data_dir / "dev-ca" / "ca.crt"
    if not ca_path.is_file():
        raise CliError(f"CA certificate not found: {ca_path}")
    ssid = password = None
    if not args.no_wifi:
        ssid = args.ssid or input("Wi-Fi SSID (2.4 GHz): ")
        password = getpass.getpass("Wi-Fi password (not echoed): ")
    targets: list[dict] = []
    if not args.no_targets:
        if args.target_file:
            targets = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.target_file]
        elif args.direct:
            store = _store(s)
            targets = [t.model_dump(mode="json") for t in store.list_targets()]
            store.close()
        else:
            targets = _call(s, args, "GET", "/v1/admin/targets").json()["targets"]
    plan = ProvisionPlan(
        device_id=args.id,
        backend_host=host,
        backend_port=args.backend_port or s.https_port,
        wifi_ssid=ssid,
        wifi_password=password,
        ca_pem=ca_path.read_text(encoding="ascii"),
        targets=targets,
        clear_targets=not args.no_targets,
        max_line=4096 if args.allow_long_lines else MAX_LINE,
    )

    def token_source() -> str:
        if args.token_file:
            return Path(args.token_file).read_text(encoding="utf-8").strip()
        return enroll_device(s, args, args.id, args.name, False)

    try:
        link = SerialLink(args.port, echo=print)
    except Exception as e:  # pyserial raises SerialException (an OSError subclass on most platforms)
        raise CliError(f"cannot open {args.port}: {e}") from None
    try:
        status = provision(link, plan, token_source)
    except ProvisioningError as e:
        raise CliError(f"provisioning failed (staged changes aborted): {e}") from None
    finally:
        link.close()
    print(f"provisioned {args.id}: {json.dumps(status)}")
    return 0


# --- parser / entry point ---
def _ai_missing(name: str) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        _err(f"unsupported: `{name}` needs the AI package (oyster.ai.cli), which is not installed")
        return 1

    return _handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oyster", description="Oyster Pocket backend tools")
    parser.add_argument("--env-file", help="settings file (default backend/.env)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for cmd in REGISTRY.values():
        p = sub.add_parser(cmd.name, help=cmd.help, description=cmd.help)
        cmd.configure(p)
        p.set_defaults(func=cmd.handler)
    if _ai_register is not None:
        _ai_register(sub)
    for name, help_text in AI_COMMANDS.items():
        if name not in sub.choices:
            p = sub.add_parser(name, help=help_text + " [unavailable]")
            p.add_argument("rest", nargs=argparse.REMAINDER)
            p.set_defaults(func=_ai_missing(name))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except CliError as e:
        _err(f"error: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
