// Direct-AI wire format: request builder and reply parser. See directai_parse.h for the rules.
#include "oyster_core/directai_parse.h"

#include <ArduinoJson.h>

#include <cstring>

#include "oyster_core/json_writer.h"

namespace oyster::directai {

// No device tools in this mode; the assistant answers from knowledge only. Plain ASCII (no quotes or
// control characters) so it needs no escaping and carries no vendor or tool names.
const char kSystemPrompt[] =
    "You are the on-device assistant of a handheld security companion. Answer the user's general "
    "question from your own knowledge, concisely, in a few short sentences of plain markdown. In this "
    "mode you have no device tools and cannot run scans, Wi-Fi surveys or any device checks here: if "
    "asked to scan, measure or inspect a device, say that must be done from the instrument's own scan "
    "tools, not in this chat. Do not invent specific measurements, identifiers or results.";

namespace {

// Copy display text into a bounded buffer: control characters become spaces, the cut never splits a
// UTF-8 sequence. Mirrors copy_excerpt() in backend_client.cpp so on-screen text behaves the same.
void copy_clip(char* out, size_t cap, const char* in) {
  if (cap == 0) return;
  size_t n = 0;
  for (const char* p = in; p != nullptr && *p != '\0' && n + 1 < cap; ++p) {
    const unsigned char c = static_cast<unsigned char>(*p);
    out[n++] = c < 0x20 ? ' ' : static_cast<char>(c);
  }
  out[n] = '\0';
  if (in != nullptr && std::strlen(in) > n) {
    while (n > 0 && (static_cast<unsigned char>(out[n - 1]) & 0xC0) == 0x80) --n;  // continuation bytes
    if (n > 0 && (static_cast<unsigned char>(out[n - 1]) & 0xC0) == 0xC0) --n;     // dangling lead byte
    out[n] = '\0';
  }
}

}  // namespace

size_t build_chat_body(const char* model, const char* question, char* buf, size_t cap) {
  if (model == nullptr || model[0] == '\0' || question == nullptr || question[0] == '\0' || buf == nullptr) {
    if (buf != nullptr && cap > 0) buf[0] = '\0';
    return 0;
  }
  JsonWriter w(buf, cap);
  w.begin_object();
  w.str("model", model);
  w.begin_array("messages");
  w.begin_object();  // system
  w.str("role", "system");
  w.str("content", kSystemPrompt);
  w.end_object();
  w.begin_object();  // user
  w.str("role", "user");
  w.str("content", question);
  w.end_object();
  w.end_array();
  w.num("max_completion_tokens", kMaxCompletionTokens);
  w.raw("temperature", "0.2");  // trusted literal; JsonWriter has no float writer
  w.end_object();
  if (!w.ok()) {
    if (cap > 0) buf[0] = '\0';
    return 0;
  }
  return w.size();
}

bool parse_chat_answer(const char* json, char* out, size_t cap) {
  if (out == nullptr || cap == 0) return false;
  out[0] = '\0';
  if (json == nullptr) return false;
  JsonDocument doc;
  if (deserializeJson(doc, json) != DeserializationError::Ok) return false;

  // An error body wins over any content: surface its message (never the key; the key is a transport
  // header and never reaches this reply).
  JsonVariantConst err = doc["error"];
  if (!err.isNull()) {
    const char* msg = err["message"].as<const char*>();
    if (msg == nullptr) msg = err.as<const char*>();  // some servers send error as a bare string
    if (msg != nullptr) copy_clip(out, cap, msg);
    return false;
  }

  // choices[0].message.content; any missing level or wrong type yields a null variant safely.
  const char* content = doc["choices"][0]["message"]["content"].as<const char*>();
  if (content == nullptr || content[0] == '\0') return false;
  copy_clip(out, cap, content);
  return out[0] != '\0';
}

}  // namespace oyster::directai
