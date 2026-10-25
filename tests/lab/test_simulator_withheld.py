"""The withheld scenario is blind: it resolves to one of two targets by seed, records the choice only
in a sealed file next to the session file, and never prints it to stdout."""

from __future__ import annotations

from _simulator_backend import FakeBackend
from oyster_lab.simulator import __main__ as cli
from oyster_lab.simulator.scenarios import WITHHELD_CHOICES, list_scenarios, load_scenario


def test_withheld_is_listed():
    assert "withheld" in list_scenarios()


def test_withheld_resolution_is_seeded_and_valid():
    a = load_scenario("withheld", seed=0)
    b = load_scenario("withheld", seed=0)
    assert a.resolved_choice == b.resolved_choice
    assert a.resolved_choice in WITHHELD_CHOICES
    assert a.name == "withheld"  # the public name never reveals the pick
    # The two seeds below pick different targets, proving the choice really varies.
    picks = {load_scenario("withheld", seed=s).resolved_choice for s in range(8)}
    assert picks == set(WITHHELD_CHOICES)


def test_withheld_choice_not_leaked_to_stdout_and_sealed(tmp_path, monkeypatch, capsys):
    backend = FakeBackend()
    backend.auto_stamp_session = True
    scenario = load_scenario("withheld", seed=0)
    for raw in scenario.plan:
        job = dict(raw)
        job["schema_version"] = "1.0"
        job["session_id"] = "sess-placeholder-001"
        backend.enqueue(job)

    prebuilt = backend.client()  # built with the real httpx.Client before patching
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: prebuilt)

    token_file = tmp_path / "token.txt"
    token_file.write_text("dev-token-abc", encoding="utf-8")
    session_file = tmp_path / "session.txt"

    rc = cli.main(
        [
            "run",
            "--scenario", "withheld",
            "--backend", "http://backend.test",
            "--token-file", str(token_file),
            "--session-file", str(session_file),
            "--auto-confirm",
            "--seed", "0",
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out
    # The resolved target name must not appear anywhere on stdout.
    assert scenario.resolved_choice not in out
    assert "vulnerable_target" not in out
    assert "corrected_target" not in out

    sealed = session_file.with_suffix(session_file.suffix + ".withheld.sealed")
    assert sealed.is_file()
    assert sealed.read_text(encoding="utf-8").strip() == scenario.resolved_choice
    assert session_file.read_text(encoding="utf-8").strip().startswith("sess-")


def test_scenarios_subcommand_lists_names(capsys, monkeypatch):
    rc = cli.main(["scenarios"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "success" in out
    assert "withheld" in out


def test_cli_run_smoke_against_fake_backend(tmp_path, monkeypatch, capsys):
    backend = FakeBackend()
    backend.auto_stamp_session = True
    scenario = load_scenario("success")
    for raw in scenario.plan:
        job = dict(raw)
        job["schema_version"] = "1.0"
        job["session_id"] = "sess-placeholder-001"
        backend.enqueue(job)
    prebuilt = backend.client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: prebuilt)
    token_file = tmp_path / "token.txt"
    token_file.write_text("dev-token", encoding="utf-8")
    rc = cli.main(
        ["run", "--scenario", "success", "--backend", "http://backend.test", "--token-file", str(token_file)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "scenario: success" in out
    assert "jobs executed: 5" in out


def test_simulator_does_not_import_backend_code():
    # The simulator is an independent device-side implementation; it must not import the backend
    # package (oyster.*). It only uses httpx + jsonschema + schemas/.
    import pathlib

    import oyster_lab.simulator as pkg

    src_dir = pathlib.Path(pkg.__file__).parent
    for py in src_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "import oyster\n" not in text and "from oyster " not in text, py.name
        assert "from oyster." not in text and "import oyster." not in text, py.name
