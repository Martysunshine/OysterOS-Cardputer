// Direct-AI client: one single-turn chat request to an OpenAI-compatible endpoint (Groq) over TLS,
// so the handheld can answer a general question with no laptop/backend. v1 is Q&A only: no device
// tool-calling, no evidence; the backend path (backend_client.cpp) stays the richer mode.
//
// ask() runs on the caller's task (the worker task, the one network owner) and blocks for the request.
// It opens its own validated WiFiClientSecure with the caller-supplied CA (never setInsecure). The API
// key is taken through Config, lives only in the outgoing Authorization header, and never appears in
// err_out, the answer, or any log. All buffers are bounded; nothing is allocated unbounded.
//
// Everything the client needs is passed in Config by the caller (the lead wires it from stored
// settings); this unit never reads NVS/LittleFS itself. The pure request/response wire format is in
// oyster_core/directai_parse.h and is host-tested.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::directai {

enum class Status : uint8_t {
  Ok,            // answer_out holds the reply
  NotConfigured, // a required Config field is missing (host/port/path/model/key/CA)
  NoLink,        // Wi-Fi is not connected
  TlsError,      // could not open a validated TLS connection (handshake or certificate rejected)
  HttpError,     // the server answered with a non-200 status
  Timeout,       // the request did not complete within cfg.timeout_ms
  BadResponse,   // the reply was not a usable chat completion
  TooLarge,      // the question would not fit the request buffer, or the reply exceeded the cap
};

// Short human-readable label for a Status (for logs/UI). Never contains the key.
const char* to_string(Status s);

// Connection parameters. The caller owns every pointer; they must outlive the ask() call. Defaults the
// caller should use: host "api.groq.com", port 443, path "/openai/v1/chat/completions".
struct Config {
  const char* host;      // e.g. "api.groq.com"
  uint16_t port;         // e.g. 443
  const char* path;      // e.g. "/openai/v1/chat/completions"
  const char* model;     // the model id to request
  const char* api_key;   // bearer token; sent only in the Authorization header, never logged
  const char* ca_pem;    // PEM root(s) to validate the host (e.g. default_ca_pem() or a stored override)
  uint32_t timeout_ms;   // overall budget for connect + request + response (0 -> a built-in default)
};

// Send one question and return the answer. On Status::Ok `answer_out` holds the reply text (control
// characters folded to spaces, clipped to answer_cap-1 on a UTF-8 boundary). On any failure `err_out`
// holds a short, key-free explanation. Both buffers are always NUL-terminated when non-empty.
Status ask(const Config& cfg, const char* question, char* answer_out, size_t answer_cap, char* err_out,
           size_t err_cap);

// The built-in root CA bundle (groq_ca.h). The caller may pass this as Config::ca_pem when no stored
// override exists. Unverified against the live host; see groq_ca.h.
const char* default_ca_pem();

}  // namespace oyster::directai
