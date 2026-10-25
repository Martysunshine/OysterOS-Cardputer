"""Key-sequence replay (`--script keys.txt`): one command per line, '#' starts a comment.

profile firmware|preview     fresh simulator (preview = also enables the Original Apps tile)
conn offline|connect|online|no-clock|cert-err|auth-err|init-err
scan normal|zero             what simulated scans find
fault ble                    the BLE module "did not start"
outcome <job> <result>       job = wifi ble gatt ai deck ir; result = success failed timed_out unknown
goto <route> [state]         jump with a Back chain (state = normal loading error empty disabled)
state <state>                force a state variant on the current route
key <chord> [<chord> ...]    physical key chords: down enter fn+del ctrl+c ? ` 7 (see keys.parse_chord)
type <text>                  printable characters (text entry, hotkeys)
wait <ms>                    advance the simulation clock
event confirm                the mock backend asks for a local confirmation
shot <name>                  save the frame as <out>/<name>.png
expect route <id> | modal <none|route-id> | text <substring> | state <state>   (fails the replay)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .engine import JOB_KINDS, Simulator
from .keys import ChordError
from .model import ConnState
from .routes import BY_ID, STATES

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ScriptError(RuntimeError):
    def __init__(self, line_no: int, message: str) -> None:
        super().__init__(f"line {line_no}: {message}")
        self.line_no = line_no


@dataclass
class ScriptResult:
    frames: list[Path] = field(default_factory=list)
    checks: int = 0
    sim: Simulator | None = None


def _conn(name: str) -> ConnState:
    for c in ConnState:
        if c.value == name or c.name.lower() == name.lower():
            return c
    raise ValueError(f"unknown connectivity {name!r}")


def screen_text(sim: Simulator) -> str:
    """All text of the last frame (heading, rows, footer) for `expect text`."""
    log = sim.ui.log
    return "\n".join([log.status, log.heading, *log.rows, log.footer])


def run_script(text: str, out_dir: Path | None = None, sim: Simulator | None = None) -> ScriptResult:
    sim = sim or Simulator()
    res = ScriptResult(sim=sim)
    for n, raw in enumerate(text.splitlines(), 1):
        body = raw.strip()
        if body.startswith("type "):  # the rest of the line is literal text, '#' included
            cmd, rest = "type", body[len("type ") :]
        else:
            body = raw.split("#", 1)[0].strip()
            if not body:
                continue
            cmd, _, rest = body.partition(" ")
            cmd, rest = cmd.lower(), rest.strip()
        try:
            sim = _command(sim, res, cmd, rest, out_dir)
        except (ChordError, ValueError, KeyError, AssertionError) as exc:
            raise ScriptError(n, str(exc) or type(exc).__name__) from exc
        res.sim = sim
    return res


def _command(sim: Simulator, res: ScriptResult, cmd: str, rest: str, out_dir: Path | None) -> Simulator:
    args = rest.split()
    if cmd == "profile":
        if rest not in ("firmware", "preview"):
            raise ValueError("profile must be firmware or preview")
        return Simulator(preview=rest == "preview")
    elif cmd == "conn":
        sim.set_connectivity(_conn(rest))
    elif cmd == "scan":
        if rest not in ("normal", "zero"):
            raise ValueError("scan must be normal or zero")
        sim.scan_data = rest
    elif cmd == "fault":
        sim.faults.add(rest)
    elif cmd == "outcome":
        if len(args) != 2 or args[0] not in JOB_KINDS:
            raise ValueError(f"outcome <{'|'.join(JOB_KINDS)}> <result>")
        sim.outcomes[args[0]] = args[1]
    elif cmd == "goto":
        if not args or args[0] not in BY_ID or (len(args) > 1 and args[1] not in STATES):
            raise ValueError(f"goto <route> [state]; routes: {', '.join(BY_ID)}")
        sim.goto(args[0], args[1] if len(args) > 1 else None)
    elif cmd == "state":
        if rest not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        sim.forced = rest
    elif cmd == "key":
        if not args:
            raise ValueError("key needs at least one chord")
        for chord in args:
            sim.press(chord)
    elif cmd == "type":
        sim.type_text(rest)
    elif cmd == "wait":
        sim.advance(int(rest))
    elif cmd == "event":
        if rest != "confirm":
            raise ValueError("event confirm")
        sim.request_confirmation(
            "ble_inspect_gatt", "lab-01", "Lists the services of the enrolled lab peripheral.", "ai_pending"
        )
    elif cmd == "shot":
        if not _NAME.match(rest):
            raise ValueError("shot <name> (letters, digits, _ . -)")
        sim.render()
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{rest}.png"
            sim.ui.screen.image(4).save(path)
            res.frames.append(path)
    elif cmd == "expect":
        _expect(sim, args, rest)
        res.checks += 1
    else:
        raise ValueError(f"unknown command {cmd!r}")
    return sim


def _expect(sim: Simulator, args: list[str], rest: str) -> None:
    if len(args) < 2:
        raise ValueError("expect route|modal|text|state <value>")
    what, value = args[0], rest.split(None, 1)[1]
    if what == "route":
        assert sim.cur == value, f"expected route {value}, on {sim.cur}"
    elif what == "modal":
        have = sim.modal.route if sim.modal else "none"
        assert have == value, f"expected modal {value}, have {have}"
    elif what == "state":
        assert sim.state_of() == value, f"expected state {value}, have {sim.state_of()}"
    elif what == "text":
        sim.render()
        assert value in screen_text(sim), f"text {value!r} not on screen:\n{screen_text(sim)}"
    else:
        raise ValueError("expect route|modal|text|state <value>")
