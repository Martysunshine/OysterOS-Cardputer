[OysterOS](../../README.md) / [Docs](../README.md) / ADR-002

# ADR-002: AI provider adapter

**Status:** accepted (2026-10-24)

**Decision.** One `Provider` interface (`backend/oyster/ai/provider.py`). The real implementation is an
OpenAI-compatible HTTP adapter used with **Groq** (`AI_PROVIDER=groq`, base `https://api.groq.com/openai/v1`).
A `FakeProvider` exists for tests and simulation only and is always labelled `is_fake=true`.

**Why.** The project uses Groq. Its API is OpenAI-compatible, so the same adapter serves OpenAI, OpenRouter
and local servers by changing `AI_BASE_URL`/`AI_MODEL`.

**Consequences.**
- **Tool calling only.** Groq cannot combine `response_format` structured outputs with tools in one
  request, so the orchestrator drives everything through tool calls (the catalogue tools plus a
  `final_answer` tool) and validates each against `schemas/ai-action.schema.json`.
- `tool_choice: "auto"`; no `name` field on `tool` messages (Groq rejects `messages[].name`); `n` is 1.
- The model id is **not** baked in. `oyster ai-check` lists `/models`, runs one tool-call round trip, and
  records the working model in `data/provider_check.json`. Tool-capable production candidates today:
  `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`.
- Keys live only in `backend/.env`, never on the device, SD, or in logs. Provider unavailability surfaces
  "AI unavailable"; it never silently switches providers or fabricates a reply.
