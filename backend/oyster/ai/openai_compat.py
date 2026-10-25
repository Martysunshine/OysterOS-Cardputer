"""OpenAI-compatible chat-completions adapter, used with Groq (ADR-002).

Groq specifics honoured here (checked against Groq's current API docs):
  - POST {AI_BASE_URL}/chat/completions with `tools` (type "function") and `tool_choice: "auto"`.
  - Tool calls arrive in choices[0].message.tool_calls[].function.{name, arguments (JSON string)}.
  - Tool results go back as {"role": "tool", "tool_call_id", "content"}; no `name` on any message
    (Groq rejects messages[].name), `n` is never sent (must be 1), no logprobs / logit_bias.
  - Structured outputs cannot be combined with tools, so everything is a tool call (incl. final_answer).
  - A 400 with error.code "tool_use_failed" means the MODEL produced an invalid tool call: it is
    returned as a parse error (eligible for one schema repair), not as a transport failure.
HTTPS verification uses the OS trust store (truststore). The API key is sent only in the Authorization
header and is scrubbed from every error message.
"""

from __future__ import annotations

import ipaddress
import json
import re
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
import truststore
from pydantic import ValidationError

from oyster.ai.provider import Provider, ProviderError, ProviderMessage, ProviderResult, ToolSpec, Usage
from oyster.ai.redact import clip, redact_text
from oyster.contracts import AiAction, FinalAnswer

_ROLES = ("system", "user", "assistant", "tool")
MAX_PROSE_CHARS = 4000
MAX_TOOL_NAME = 64


def tls_context() -> ssl.SSLContext:
    """OS trust store context (honours locally trusted roots; verification stays on)."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def check_base_url(base_url: str) -> None:
    """HTTPS required; plain HTTP only for a loopback server (local OpenAI-compatible runtimes)."""
    u = urlparse(base_url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname:
        host = u.hostname
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise ValueError("AI_BASE_URL must be https:// (plain http only for a loopback server)")


# --- wire conversion ---
def wire_tool_call(tc: dict) -> dict:
    fn = tc.get("function") or {}
    args = fn.get("arguments", "{}")
    if not isinstance(args, str):
        args = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return {
        "id": str(tc["id"]),
        "type": "function",
        "function": {"name": str(fn.get("name", "")), "arguments": args},
    }


def wire_message(m: ProviderMessage) -> dict:
    """ProviderMessage -> chat-completions message. Never emits a `name` field."""
    if m.role not in _ROLES:
        raise ValueError(f"unsupported message role {m.role!r}")
    if m.role == "tool":
        if not m.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    if m.role == "assistant" and m.tool_calls:
        return {
            "role": "assistant",
            "content": m.content or None,
            "tool_calls": [wire_tool_call(tc) for tc in m.tool_calls],
        }
    return {"role": m.role, "content": m.content}


def wire_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def parse_chat_message(message: dict) -> tuple[list[AiAction], str, str | None]:
    """Normalize one assistant message into AiActions.

    Every tool call yields exactly one AiAction (so every call id can be answered). Malformed pieces
    leave `arguments`/`final` as None and are described in the returned parse error. Error text never
    includes argument values (model output may echo untrusted observation data).
    """
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    actions: list[AiAction] = []
    errors: list[str] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        if not isinstance(tc, dict):
            errors.append(f"tool call #{i} is not an object")
            continue
        call_id = clip(str(tc.get("id") or f"call_{i}"), 120)
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name")
        raw = fn.get("arguments")
        args: dict | None = None
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                errors.append(f"tool call #{i}: arguments are not valid JSON")
                parsed = None
            if parsed is not None and not isinstance(parsed, dict):
                errors.append(f"tool call #{i}: arguments must be a JSON object")
                parsed = None
            args = parsed
        else:
            errors.append(f"tool call #{i}: missing arguments")
        if not isinstance(name, str) or not name:
            errors.append(f"tool call #{i}: missing function name")
            name = ""
        name = name[:MAX_TOOL_NAME]
        if name == "final_answer":
            final = None
            if args is not None:
                try:
                    final = FinalAnswer.model_validate(args)
                except ValidationError:
                    errors.append(f"tool call #{i}: final_answer arguments do not match the schema")
            actions.append(
                AiAction(kind="final_answer", arguments=args, final=final, provider_call_id=call_id)
            )
        else:
            actions.append(AiAction(kind="tool_call", tool=name, arguments=args, provider_call_id=call_id))
    parse_error = "; ".join(errors) if errors else None
    if not actions and parse_error is None:
        parse_error = "response contained no tool call"
    return actions, text[:MAX_PROSE_CHARS], parse_error


def usage_from(data: dict) -> Usage:
    u = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def _int(v) -> int:
        return v if isinstance(v, int) and v >= 0 else 0

    return Usage(
        input_tokens=_int(u.get("prompt_tokens")),
        output_tokens=_int(u.get("completion_tokens")),
        provider_requests=1,
    )


class ProviderRateLimited(ProviderError):
    """HTTP 429 that was not (or could not be) waited out. retry_after_s is the provider's hint, if any."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


# Provider hints look like "Please try again in 10.335s", "in 7m12.96s" or "in 460ms".
_TRY_AGAIN = re.compile(
    r"try again in\s+(?:(\d+(?:\.\d+)?)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?\s*(?:(\d+(?:\.\d+)?)ms)?", re.I
)
_ORG_ID = re.compile(r"\borg_[A-Za-z0-9]{8,}\b")


def retry_after_seconds(header_value: str | None, message: str) -> float | None:
    """Seconds the provider asks us to wait: the Retry-After header wins, else the hint in the message."""
    if header_value:
        try:
            v = float(header_value.strip())
            if v >= 0:
                return v
        except ValueError:
            pass
    m = _TRY_AGAIN.search(message or "")
    if m and any(m.groups()):
        minutes, seconds, millis = (float(g) if g else 0.0 for g in m.groups())
        return minutes * 60.0 + seconds + millis / 1000.0
    return None


class _ToolUseFailed(Exception):
    pass


class OpenAICompatProvider(Provider):
    """Real HTTP provider. One instance per backend process; thread-safe (httpx.Client)."""

    is_fake = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str = "groq",
        timeout_s: float = 60.0,
        max_output_tokens: int = 1500,
        temperature: float = 0.2,
        transport: httpx.BaseTransport | None = None,
        final_tool: ToolSpec | None = None,
        max_rate_limit_retries: int = 2,
        max_retry_wait_s: float = 45.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("AI_API_KEY is not set")
        check_base_url(base_url)
        self.name = name
        self.model = model or None
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_output_tokens = int(max_output_tokens)
        self.temperature = temperature
        self._secret = api_key
        self._final_tool = final_tool
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self.max_retry_wait_s = float(max_retry_wait_s)
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_s),
            verify=tls_context(),
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            follow_redirects=False,
        )

    # --- Provider interface ---
    def propose_action(self, messages: list[ProviderMessage], tools: list[ToolSpec]) -> ProviderResult:
        return self._complete(messages, tools)

    def explain_results(self, messages: list[ProviderMessage]) -> ProviderResult:
        return self._complete(messages, [self._final_answer_tool()])

    # --- HTTP ---
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """One POST /chat/completions. Returns the decoded JSON body or raises ProviderError."""
        use_model = model or self.model
        if not use_model:
            raise ProviderError("AI_MODEL is not set (run `oyster ai-check`)")
        body: dict = {
            "model": use_model,
            "messages": messages,
            "max_completion_tokens": int(max_tokens or self.max_output_tokens),
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = self._send("POST", "/chat/completions", body)
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned a non-JSON response") from e
        if not isinstance(data, dict):
            raise ProviderError("provider returned an unexpected response shape")
        return data

    def list_models(self) -> list[dict]:
        resp = self._send("GET", "/models", None)
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned a non-JSON model list") from e
        items = data.get("data") if isinstance(data, dict) else None
        return (
            [m for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]
            if isinstance(items, list)
            else []
        )

    def close(self) -> None:
        self._client.close()

    # --- internals ---
    def _final_answer_tool(self) -> ToolSpec:
        if self._final_tool is None:
            from oyster.ai.tools import final_answer_spec, load_lab_profile

            self._final_tool = final_answer_spec(load_lab_profile())
        return self._final_tool

    def _scrub(self, text: str) -> str:
        return clip(_ORG_ID.sub("org_<redacted>", redact_text(text, (self._secret,))), 300)

    def _send(self, method: str, path: str, body: dict | None) -> httpx.Response:
        attempts = 0
        while True:
            resp = self._request(method, path, body)
            if resp.status_code == 200:
                return resp
            code, message = self._error_info(resp)
            if resp.status_code == 429:
                wait = retry_after_seconds(resp.headers.get("retry-after"), message)
                can_wait = wait is not None and wait <= self.max_retry_wait_s
                if can_wait and attempts < self.max_rate_limit_retries:
                    attempts += 1
                    self._sleep(wait + 0.5)  # small margin: the provider's window is rolling
                    continue
                hint = f"; retry in about {wait:.0f} s" if wait is not None else ""
                raise ProviderRateLimited(
                    "provider rate limit (HTTP 429" + (f" {code}" if code else "") + ")" + hint
                    + (f": {message}" if message else ""),
                    retry_after_s=wait,
                )
            if resp.status_code == 400 and code == "tool_use_failed":
                raise _ToolUseFailed(message)
            raise ProviderError(
                f"provider HTTP {resp.status_code}"
                + (f" {code}" if code else "")
                + (f": {message}" if message else "")
            )

    def _request(self, method: str, path: str, body: dict | None) -> httpx.Response:
        try:
            if method == "GET":
                return self._client.get(path)
            return self._client.post(path, json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"provider timeout after {self.timeout_s:g} s ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            raise ProviderError(
                f"provider transport error ({type(e).__name__}): {self._scrub(str(e))}"
            ) from e

    def _error_info(self, resp: httpx.Response) -> tuple[str, str]:
        try:
            data = resp.json()
        except ValueError:
            return "", ""
        err = data.get("error") if isinstance(data, dict) else None
        if not isinstance(err, dict):
            return "", ""
        code = err.get("code") if isinstance(err.get("code"), str) else ""
        msg = err.get("message") if isinstance(err.get("message"), str) else ""
        return clip(code, 60), self._scrub(msg)

    def _complete(self, messages: list[ProviderMessage], tools: list[ToolSpec]) -> ProviderResult:
        wire = [wire_message(m) for m in messages]
        try:
            data = self.chat(wire, wire_tools(tools))
        except _ToolUseFailed as e:
            return ProviderResult(
                actions=[],
                usage=Usage(provider_requests=1),
                parse_error=f"provider rejected the model's tool call (tool_use_failed): {e}",
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("provider response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("provider response has no message")
        actions, text, parse_error = parse_chat_message(message)
        if not actions and choices[0].get("finish_reason") == "length":
            parse_error = "output token cap reached before a tool call"
        return ProviderResult(actions=actions, usage=usage_from(data), raw_text=text, parse_error=parse_error)
