"""FakeProvider: deterministic, scripted provider for tests and simulation only (ADR-002).

Always reports is_fake=True; runs record it and answers carry a limitation saying no language model
was used. Script steps are processed in order, one per provider request:
  - an OpenAI-style assistant message dict (see tool_call()/final()/prose()), parsed by the same code
    as the real adapter; an optional "_usage": {"prompt_tokens", "completion_tokens"} sets usage;
  - a ProviderResult (returned as is);
  - an exception instance (raised, e.g. ProviderError for a simulated timeout);
  - a callable(messages, tools) returning any of the above.
With script=None the provider follows a small keyword policy for the simulator (see _simulation_step).
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import threading

from oyster.ai.openai_compat import parse_chat_message
from oyster.ai.provider import Provider, ProviderError, ProviderMessage, ProviderResult, ToolSpec, Usage

_EVT = re.compile(r"\bevt-[0-9A-Za-z_-]{1,40}\b")
_ids = itertools.count(1)


def _call_id() -> str:
    return f"call_fake_{next(_ids)}"


def tool_call(name: str, arguments: dict | None = None, call_id: str | None = None) -> dict:
    return tool_calls((name, arguments or {}, call_id))


def tool_calls(*calls: tuple) -> dict:
    """Several calls in one assistant message: tool_calls(("name", {args}), ("name2", {args}, "id"))."""
    out = []
    for c in calls:
        name, args = c[0], c[1]
        cid = c[2] if len(c) > 2 and c[2] else _call_id()
        out.append(
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, sort_keys=True)},
            }
        )
    return {"role": "assistant", "content": None, "tool_calls": out}


def raw_call(name: str, raw_arguments: str, call_id: str | None = None) -> dict:
    """A tool call whose arguments string is passed through untouched (e.g. malformed JSON)."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id or _call_id(),
                "type": "function",
                "function": {"name": name, "arguments": raw_arguments},
            }
        ],
    }


def final(
    answer: str,
    cited: list[str] | tuple = (),
    limitations: list[str] | tuple = (),
    suggestions: list[str] | tuple = (),
    call_id: str | None = None,
) -> dict:
    return tool_call(
        "final_answer",
        {
            "answer_markdown": answer,
            "cited_evidence_ids": list(cited),
            "limitations": list(limitations),
            "suggestions": list(suggestions),
        },
        call_id,
    )


def prose(text: str) -> dict:
    return {"role": "assistant", "content": text}


def evidence_ids_in(messages: list[ProviderMessage], *, last_only: bool = False) -> list[str]:
    """Evidence ids appearing in tool results (in order, de-duplicated)."""
    tool_msgs = [m for m in messages if m.role == "tool"]
    if last_only:
        tool_msgs = tool_msgs[-1:]
    seen: dict[str, None] = {}
    for m in tool_msgs:
        for e in _EVT.findall(m.content):
            seen.setdefault(e, None)
    return list(seen)


class FakeProvider(Provider):
    name = "fake"

    def __init__(
        self, script: list | None = None, *, model: str | None = None, usage: tuple[int, int] = (0, 0)
    ):
        self.is_fake = True
        self.model = model
        self._script = list(script) if script is not None else None
        self._usage = usage
        self._lock = threading.Lock()
        self.calls: list[dict] = []  # {"method", "messages", "tools"} snapshots for test inspection

    def propose_action(self, messages: list[ProviderMessage], tools: list[ToolSpec]) -> ProviderResult:
        return self._next("propose_action", messages, tools)

    def explain_results(self, messages: list[ProviderMessage]) -> ProviderResult:
        from oyster.ai.tools import final_answer_spec, load_lab_profile

        return self._next("explain_results", messages, [final_answer_spec(load_lab_profile())])

    @property
    def remaining(self) -> int | None:
        return None if self._script is None else len(self._script)

    def _next(self, method: str, messages: list[ProviderMessage], tools: list[ToolSpec]) -> ProviderResult:
        with self._lock:
            self.calls.append(
                {"method": method, "messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
            )
            if self._script is None:
                step = _simulation_step(messages, tools)
            elif not self._script:
                raise ProviderError("fake provider script exhausted")
            else:
                step = self._script.pop(0)
        if callable(step) and not isinstance(step, BaseException):
            step = step(messages, tools)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ProviderResult):
            return step
        if isinstance(step, dict):
            msg = dict(step)
            u = msg.pop("_usage", None) or {}
            actions, text, perr = parse_chat_message(msg)
            usage = Usage(
                input_tokens=int(u.get("prompt_tokens", self._usage[0])),
                output_tokens=int(u.get("completion_tokens", self._usage[1])),
                provider_requests=1,
            )
            return ProviderResult(actions=actions, usage=usage, raw_text=text, parse_error=perr)
        raise TypeError(f"unsupported fake script step {type(step).__name__}")


def _tool_enum(tools: list[ToolSpec], name: str, prop: str) -> list[str]:
    for t in tools:
        if t.name == name:
            return list(t.parameters.get("properties", {}).get(prop, {}).get("enum", []))
    return []


def _inspect_and_read_plan(question: str, tools: list[ToolSpec], offered: set[str]) -> list[tuple[str, dict]]:
    """GATT inspection and/or a read of the first PUBLIC readable characteristic (lab profile policy)."""
    from oyster.ai.tools import load_lab_profile, public_read_characteristics

    plan: list[tuple[str, dict]] = []
    if any(k in question for k in ("gatt", "inspect", "service")) and "ble_inspect_gatt" in offered:
        plan.append(
            ("ble_inspect_gatt", {"target_id": _tool_enum(tools, "ble_inspect_gatt", "target_id")[0]})
        )
    if any(k in question for k in ("read", "value")) and "ble_read_profile_value" in offered:
        public = public_read_characteristics(load_lab_profile())
        chars = [c for c in _tool_enum(tools, "ble_read_profile_value", "characteristic_id") if c in public]
        # prefer a characteristic the question names (e.g. "sensor" -> public_sensor)
        chars.sort(key=lambda c: not any(w in question for w in c.split("_") if w != "public"))
        if chars:
            target = _tool_enum(tools, "ble_read_profile_value", "target_id")[0]
            plan.append(("ble_read_profile_value", {"target_id": target, "characteristic_id": chars[0]}))
    return plan


def _simulation_step(messages: list[ProviderMessage], tools: list[ToolSpec]) -> dict:
    """Keyword policy used by the simulator when AI_PROVIDER=fake. Deterministic; no model involved."""
    question = next((m.content for m in messages if m.role == "user"), "").lower()
    offered = {t.name for t in tools}
    plan: list[tuple[str, dict]] = []
    if any(k in question for k in ("wifi", "wi-fi", "survey")) and "wifi_survey" in offered:
        plan = [("wifi_survey", {})]
    elif (
        any(k in question for k in ("access", "policy", "assess", "check"))
        and "ble_check_access_policy" in offered
    ):
        targets = _tool_enum(tools, "ble_check_access_policy", "target_id")
        tests = _tool_enum(tools, "ble_check_access_policy", "test_id")
        plan = [("ble_check_access_policy", {"target_id": targets[0], "test_id": t}) for t in tests[:4]]
    elif inspect_read := _inspect_and_read_plan(question, tools, offered):
        plan = inspect_read
    elif any(k in question for k in ("discover", "ble", "bluetooth")) and "ble_discover" in offered:
        plan = [("ble_discover", {})]
    done = (
        sum(len(m.tool_calls) for m in messages if m.role == "assistant" and m.tool_calls) - 1
    )  # minus overview
    if offered != {"final_answer"} and 0 <= done < len(plan):
        name, args = plan[done]
        return tool_call(name, args)
    cited = evidence_ids_in(messages)[-8:]
    classes = sorted(
        set(
            re.findall(
                r'"classification":\s*"([a-z_]+)"', " ".join(m.content for m in messages if m.role == "tool")
            )
        )
    )
    text = (
        f"Simulated assistant (FakeProvider, no language model). {max(done, 0)} check(s) ran in this run. "
        + (f"Deterministic classifications seen: {', '.join(classes)}. " if classes else "")
        + "See the cited evidence for details."
    )
    return final(text, cited)  # the orchestrator adds the authoritative FakeProvider limitation
