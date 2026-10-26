// Pure, host-testable pieces of the direct-AI client (see firmware/include/direct_ai.h): build the
// chat-completions request body and parse the reply. No Arduino headers, no TLS, no secrets: the API
// key lives only in the transport layer (direct_ai.cpp) and never passes through here.
//
// Wire shape (OpenAI-compatible, as Groq serves it; see backend/oyster/ai/openai_compat.py):
//   request  {"model","messages":[{role,content}...],"max_completion_tokens","temperature"}
//   reply    choices[0].message.content   (string), or {"error":{"message":...}} on failure.
// Everything in the reply is untrusted input: parsers tolerate missing fields, wrong types, arrays,
// huge or non-JSON input, and never write past the caller's buffer.
#pragma once

#include <cstddef>

namespace oyster::directai {

// System prompt sent with every request. Single-turn mode has no device tools: it answers general
// questions from model knowledge only and must say so if asked to scan or inspect a device.
extern const char kSystemPrompt[];

// Output token ceiling and sampling temperature baked into the request body.
constexpr int kMaxCompletionTokens = 1024;

// Build the JSON request body for one single-turn question into `buf`. `model` and `question` must be
// non-null; the question is JSON-escaped (quotes, backslash, control characters and non-ASCII bytes).
// Returns the number of bytes written (not counting the NUL), or 0 on bad input or if it would not fit.
size_t build_chat_body(const char* model, const char* question, char* buf, size_t cap);

// Parse a chat-completions reply string into the answer text. Returns true and writes the answer
// (control characters folded to spaces, clipped on a UTF-8 boundary to cap-1) when
// choices[0].message.content is a non-empty string. Returns false for any other shape; when the reply
// is an {"error":{"message":...}} body the (bounded, scrubbed-of-control-chars) message is written to
// `out` so the caller can surface it. `out` is always NUL-terminated.
bool parse_chat_answer(const char* json, char* out, size_t cap);

}  // namespace oyster::directai
