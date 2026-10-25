"""Provider interface: the seam between the orchestrator and a concrete AI backend.

The orchestrator depends only on this abstract class. Two implementations ship:
  - OpenAICompatProvider (ai/openai_compat.py): real HTTP, used with Groq (AI_PROVIDER=groq).
  - FakeProvider (ai/fake.py): deterministic, for tests and simulation only; never pretends to be real.

Untrusted observation text (SSIDs, BLE names, characteristic values, imported notes) is passed only
as data inside tool results. The provider never receives raw history or secrets, and provider output
is treated as a proposal validated against ai-action.schema.json before anything executes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from oyster.contracts import AiAction


@dataclass
class ToolSpec:
    """One tool offered to the model (name + JSON-schema parameters), from the tool catalogue."""

    name: str
    description: str
    parameters: dict


@dataclass
class ProviderMessage:
    """One turn in the bounded conversation given to the provider.

    role: "system" | "user" | "assistant" | "tool". For a tool result, tool_call_id links it to the
    assistant tool call; content is JSON text (a normalized, redacted result), never free history.
    """

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    provider_requests: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.provider_requests += other.provider_requests


@dataclass
class ProviderResult:
    """One provider response, already normalized to zero or more candidate actions.

    The adapter parses tool calls into AiAction objects; raw_text holds any assistant prose (for
    diagnostics). parse_error is set when the response could not be normalized (triggers one repair).
    """

    actions: list[AiAction]
    usage: Usage
    raw_text: str = ""
    parse_error: str | None = None


class ProviderError(RuntimeError):
    """Transport/availability failure. Surfaced as 'AI unavailable'; never turned into a fabricated reply."""


class Provider(abc.ABC):
    name: str
    model: str | None
    is_fake: bool

    @abc.abstractmethod
    def propose_action(self, messages: list[ProviderMessage], tools: list[ToolSpec]) -> ProviderResult:
        """Ask for the next tool call or a final answer. Raises ProviderError on transport errors."""

    @abc.abstractmethod
    def explain_results(self, messages: list[ProviderMessage]) -> ProviderResult:
        """Ask the model for an evidence-grounded explanation (no tool calls)."""

    def capabilities(self) -> dict:
        """Static descriptor recorded with each run (name, model, is_fake)."""
        return {"name": self.name, "model": self.model, "is_fake": self.is_fake}
