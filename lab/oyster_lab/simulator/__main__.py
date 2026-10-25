"""`oyster-sim` command line .

    oyster-sim run --scenario NAME --backend URL --token-file F [--session-file F]
                   [--auto-confirm] [--seed N] [--max-jobs N] [--once]
                   [--serve] [--poll-interval S] [--idle-exit S]
    oyster-sim scenarios

The simulator speaks the same HTTP job/result contract as the firmware. It never prints the resolved
`withheld` choice; that is written only to a sealed file next to the session file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from .device import Simulator
from .scenarios import list_scenarios, load_scenario


def _read_token(token_file: str) -> str:
    token = Path(token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token file {token_file} is empty")
    return token


def _cmd_scenarios(_args: argparse.Namespace) -> int:
    for name in list_scenarios():
        print(name)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario, seed=args.seed)
    token = _read_token(args.token_file)
    client = httpx.Client(
        base_url=args.backend.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(args.timeout),
    )
    try:
        sim = Simulator(
            scenario,
            client,
            device_id=args.device_id,
            auto_confirm=args.auto_confirm,
        )
        session_id = sim.register_session(args.session_file)
        if args.serve:
            print(
                f"serving session {session_id}; polling every {args.poll_interval}s (Ctrl+C to stop)",
                flush=True,
            )
        report = sim.run(
            max_jobs=args.max_jobs,
            once=args.once,
            serve=args.serve,
            poll_interval_s=args.poll_interval,
            idle_exit_s=args.idle_exit,
        )
    finally:
        client.close()

    print(f"scenario: {report.scenario}")
    print(f"session:  {session_id}")
    print(f"jobs executed: {sum(jr.executed for jr in report.jobs)} of {len(report.jobs)} received")
    for jr in report.jobs:
        extra = " [duplicate skipped]" if jr.skipped_duplicate else ""
        extra += "" if jr.executed or jr.skipped_duplicate else " [not executed]"
        conf = "" if jr.confirmation_approved is None else f" confirm={jr.confirmation_approved}"
        print(f"  {jr.job_id} {jr.operation}: verdict={jr.verdict} outcome={jr.outcome}{conf}{extra}")
    # The withheld choice is intentionally NOT printed (sealed file only).
    if scenario.resolved_choice and args.session_file:
        print(f"(withheld selection sealed next to {args.session_file})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oyster-sim", description="Virtual Cardputer device simulator.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a scenario against a backend.")
    run.add_argument("--scenario", required=True, help="Scenario name (see `oyster-sim scenarios`).")
    run.add_argument("--backend", required=True, help="Backend base URL, e.g. http://127.0.0.1:8000")
    run.add_argument("--token-file", required=True, help="File containing the device bearer token.")
    run.add_argument("--session-file", default=None, help="Write the registered session id here.")
    run.add_argument("--auto-confirm", action="store_true", help="Approve local confirmations automatically.")
    run.add_argument("--seed", type=int, default=None, help="Seed for the withheld scenario pick.")
    run.add_argument("--max-jobs", type=int, default=None, help="Stop after this many jobs.")
    run.add_argument("--once", action="store_true", help="Poll and execute a single job, then stop.")
    run.add_argument("--device-id", default="sim-cardputer-01", help="Device id to register as.")
    run.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    run.add_argument(
        "--serve", action="store_true", help="Keep polling for jobs (like the firmware) until Ctrl+C."
    )
    run.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between polls with --serve.")
    run.add_argument(
        "--idle-exit", type=float, default=None, help="With --serve, stop after this many idle seconds."
    )
    run.set_defaults(func=_cmd_run)

    scenarios = sub.add_parser("scenarios", help="List available scenarios.")
    scenarios.set_defaults(func=_cmd_scenarios)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
