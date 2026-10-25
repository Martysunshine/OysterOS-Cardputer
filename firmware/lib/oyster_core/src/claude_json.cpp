// Claude Code link wire format: strict bounded parsers and the screen text. See claude_json.h for the rules.
#include "oyster_core/claude_json.h"

#include <ArduinoJson.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "oyster_core/json_writer.h"

namespace oyster::claude {

namespace {

constexpr int kNestingLimit = 8;
constexpr size_t kMinNonce = 8;

bool is_id_char(char c) {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
}

bool is_tool_char(char c) { return is_id_char(c) || c == '.' || c == ':'; }

bool is_printable(char c) { return c >= 0x20 && c <= 0x7E; }

// What the screens call the tool: never empty.
const char* tool_name(const char* tool) { return tool != nullptr && tool[0] != '\0' ? tool : "?"; }

template <typename T>
bool reject(T& out) {
  std::memset(&out, 0, sizeof(out));
  return false;
}

// The text of a JSON string value. False when the value is not a string or holds a NUL byte (\u0000): a NUL
// would end the C string early and hide the rest of what the backend sent.
bool get_string(JsonVariantConst v, const char** s, size_t* n) {
  if (!v.is<const char*>()) return false;
  const JsonString js = v.as<JsonString>();
  const char* p = js.c_str();
  if (p == nullptr) return false;
  const size_t len = std::strlen(p);
  if (len != js.size()) return false;
  *s = p;
  *n = len;
  return true;
}

// A token that must fit `cap` exactly: 1..cap-1 characters accepted by `ok`.
template <typename Pred>
bool read_token(JsonVariantConst v, char* out, size_t cap, size_t min_len, Pred ok) {
  out[0] = '\0';
  const char* s = nullptr;
  size_t n = 0;
  if (!get_string(v, &s, &n)) return false;
  if (n < min_len || n == 0 || n >= cap) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!ok(s[i])) return false;
  }
  std::memcpy(out, s, n + 1);
  return true;
}

// Display text: absent or null is empty, a string is cut to the buffer (non-printable bytes become '?'),
// any other type is refused.
bool read_display(JsonVariantConst v, char* out, size_t cap) {
  out[0] = '\0';
  if (v.isNull()) return true;
  const char* s = nullptr;
  size_t n = 0;
  if (!get_string(v, &s, &n)) return false;
  size_t i = 0;
  for (; i < n && i + 1 < cap; ++i) out[i] = is_printable(s[i]) ? s[i] : '?';
  out[i] = '\0';
  return true;
}

enum class Num : uint8_t { Absent, Ok, Bad };

Num read_uint(JsonVariantConst v, uint32_t& out) {
  if (v.isNull()) return Num::Absent;
  if (!v.is<uint32_t>()) return Num::Bad;
  out = v.as<uint32_t>();
  return Num::Ok;
}

Num read_number(JsonVariantConst v, float& out, float lo, float hi) {
  if (v.isNull()) return Num::Absent;
  if (!v.is<float>()) return Num::Bad;
  const float x = v.as<float>();
  if (!(x >= lo && x <= hi)) return Num::Bad;  // also false for NaN
  out = x;
  return Num::Ok;
}

bool parse_doc(JsonDocument& doc, const JsonDocument& filter, const char* json, size_t len) {
  if (json == nullptr || len == 0) return false;
  const DeserializationError e = deserializeJson(doc, json, len, DeserializationOption::Filter(filter),
                                                 DeserializationOption::NestingLimit(kNestingLimit));
  return e == DeserializationError::Ok && doc.is<JsonObject>();
}

bool fit(int written, size_t n) { return written > 0 && static_cast<size_t>(written) < n; }

// Copy of `in` clipped to `width` characters; the last one becomes '~' when something was cut.
void clip(const char* in, size_t width, char* out) {
  const size_t len = std::strlen(in);
  if (len <= width) {
    std::memcpy(out, in, len + 1);
    return;
  }
  if (width == 0) {
    out[0] = '\0';
    return;
  }
  std::memcpy(out, in, width);
  out[width - 1] = '~';
  out[width] = '\0';
}

// Bounded text builder: appends never write past `cap` and always leave a NUL.
class Text {
 public:
  Text(char* buf, size_t cap) : buf_(buf), cap_(cap) {
    if (cap_ > 0) buf_[0] = '\0';
  }
  void add(const char* s) {
    if (s == nullptr) return;
    while (*s != '\0' && len_ + 1 < cap_) buf_[len_++] = *s++;
    if (cap_ > 0) buf_[len_] = '\0';
  }
  __attribute__((format(printf, 2, 3))) void addf(const char* fmt, ...) {
    if (cap_ == 0 || len_ + 1 >= cap_) return;
    va_list ap;
    va_start(ap, fmt);
    const int w = std::vsnprintf(buf_ + len_, cap_ - len_, fmt, ap);
    va_end(ap);
    if (w > 0) len_ = (len_ + static_cast<size_t>(w) < cap_ - 1) ? len_ + static_cast<size_t>(w) : cap_ - 1;
  }
  void newline() {
    if (len_ > 0) add("\n");
  }
  size_t size() const { return len_; }

 private:
  char* buf_;
  size_t cap_;
  size_t len_ = 0;
};

}  // namespace

// ---- approvals -------------------------------------------------------------------------------------------

bool parse_approvals(const char* json, size_t len, ApprovalPage& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  for (const char* key : {"approval_id", "tool", "summary", "age_s", "expires_in_s", "nonce"}) {
    filter["approvals"][0][key] = true;
  }
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  JsonArrayConst list = doc["approvals"].as<JsonArrayConst>();
  if (list.isNull() || list.size() > kMaxApprovals) return reject(out);
  for (JsonObjectConst p : list) {
    Approval& dst = out.items[out.count];
    if (!read_token(p["approval_id"], dst.id, sizeof(dst.id), 1, is_id_char) ||
        !read_token(p["tool"], dst.tool, sizeof(dst.tool), 1, is_tool_char) ||
        !read_token(p["nonce"], dst.nonce, sizeof(dst.nonce), kMinNonce, is_id_char) ||
        !read_display(p["summary"], dst.summary, sizeof(dst.summary))) {
      return reject(out);
    }
    uint32_t v = 0;
    if (read_uint(p["expires_in_s"], v) != Num::Ok) return reject(out);  // without it the window is unknown
    dst.expires_in_s = v > kMaxExpiresS ? kMaxExpiresS : v;
    const Num age = read_uint(p["age_s"], v);
    if (age == Num::Bad) return reject(out);
    dst.age_s = age == Num::Ok ? v : 0;
    ++out.count;
  }
  return true;
}

// ---- usage line ------------------------------------------------------------------------------------------

bool parse_status(const char* json, size_t len, Status& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  for (const char* key : {"configured", "updated_s_ago", "model", "cost_usd", "context_pct", "five_hour_pct",
                          "seven_day_pct"}) {
    filter[key] = true;
  }
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  JsonVariantConst configured = doc["configured"];
  if (!configured.is<bool>()) return reject(out);
  if (!configured.as<bool>()) return true;  // nothing received yet: every other field stays empty
  out.configured = true;

  uint32_t age = 0;
  const Num a = read_uint(doc["updated_s_ago"], age);
  if (a == Num::Bad) return reject(out);
  out.has_age = a == Num::Ok;
  out.updated_s_ago = out.has_age ? age : 0;

  if (!read_display(doc["model"], out.model, sizeof(out.model))) return reject(out);

  const Num cost = read_number(doc["cost_usd"], out.cost_usd, 0.0f, 1000000.0f);
  const Num ctx = read_number(doc["context_pct"], out.context_pct, 0.0f, 100.0f);
  const Num five = read_number(doc["five_hour_pct"], out.five_hour_pct, 0.0f, 100.0f);
  const Num seven = read_number(doc["seven_day_pct"], out.seven_day_pct, 0.0f, 100.0f);
  if (cost == Num::Bad || ctx == Num::Bad || five == Num::Bad || seven == Num::Bad) return reject(out);
  out.has_cost = cost == Num::Ok;
  out.has_context = ctx == Num::Ok;
  out.has_five_hour = five == Num::Ok;
  out.has_seven_day = seven == Num::Ok;
  return true;
}

// ---- decision --------------------------------------------------------------------------------------------

bool parse_decide_reply(const char* json, size_t len, DecideReply& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  filter["approval_id"] = true;
  filter["state"] = true;
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  const char* state = nullptr;
  size_t n = 0;
  if (!get_string(doc["state"], &state, &n)) return reject(out);
  if (std::strcmp(state, "allowed") == 0) {
    out.state = Decided::Allowed;
  } else if (std::strcmp(state, "denied") == 0) {
    out.state = Decided::Denied;
  } else {
    return reject(out);
  }
  JsonVariantConst id = doc["approval_id"];
  if (!id.isNull() && !read_token(id, out.approval_id, sizeof(out.approval_id), 1, is_id_char)) return reject(out);
  return true;
}

// ---- request building ------------------------------------------------------------------------------------

bool path_decide(char* out, size_t n, const char* approval_id) {
  if (approval_id == nullptr || !deck::valid_name(approval_id, kIdCap)) return false;
  return fit(std::snprintf(out, n, "%s/%s/decide", kPathApprovals, approval_id), n);
}

bool body_decide(char* out, size_t n, bool allow, const char* nonce) {
  if (nonce == nullptr) return false;
  const size_t len = std::strlen(nonce);
  if (len < kMinNonce || len >= kNonceCap) return false;
  for (size_t i = 0; i < len; ++i) {
    if (!is_id_char(nonce[i])) return false;
  }
  JsonWriter w(out, n);
  w.begin_object();
  w.str("decision", allow ? "allow" : "deny");
  w.str("nonce", nonce);
  w.end_object();
  return w.ok();
}

// ---- time ------------------------------------------------------------------------------------------------

bool deadline_passed(uint32_t now_ms, uint32_t deadline_ms) { return static_cast<int32_t>(now_ms - deadline_ms) >= 0; }

uint32_t seconds_left(uint32_t now_ms, uint32_t deadline_ms) {
  if (deadline_passed(now_ms, deadline_ms)) return 0;
  return (deadline_ms - now_ms + 999u) / 1000u;
}

uint32_t deadline_from(uint32_t sent_ms, uint32_t expires_in_s) {
  if (expires_in_s > kMaxExpiresS) expires_in_s = kMaxExpiresS;
  return sent_ms + expires_in_s * 1000u;
}

// ---- screen text: entry row, approvals page ----------------------------------------------------------------

EntryState entry_state_for_failure(int status) {
  if (status == -4 || status == -5) return EntryState::Error;  // an answer came, but not a usable one
  if (status < 0) return EntryState::Offline;
  if (status == 404) return EntryState::NotSetUp;  // a backend without the Claude Code link
  if (status == 401 || status == 403) return EntryState::NotAccepted;
  return EntryState::Error;
}

void format_entry_row(EntryState st, uint8_t pending, char* out, size_t n) {
  switch (st) {
    case EntryState::Checking: std::snprintf(out, n, "Claude Code  checking..."); break;
    case EntryState::Offline: std::snprintf(out, n, "Claude Code  offline"); break;
    case EntryState::NotSetUp: std::snprintf(out, n, "Claude Code  not set up"); break;
    case EntryState::NotAccepted: std::snprintf(out, n, "Claude Code  not accepted"); break;
    case EntryState::Error: std::snprintf(out, n, "Claude Code  error"); break;
    case EntryState::Pending:
      if (pending == 0) {
        std::snprintf(out, n, "Claude Code  none pending");
      } else {
        std::snprintf(out, n, "Claude Code  %u pending", static_cast<unsigned>(pending));
      }
      break;
  }
}

void format_list_note(EntryState st, uint8_t pending, char* out, size_t n) {
  switch (st) {
    case EntryState::Checking: std::snprintf(out, n, "Checking..."); break;
    case EntryState::Offline: std::snprintf(out, n, "Backend offline. r:Retry"); break;
    case EntryState::NotSetUp: std::snprintf(out, n, "Not set up on the backend"); break;
    case EntryState::NotAccepted: std::snprintf(out, n, "Device not accepted"); break;
    case EntryState::Error: std::snprintf(out, n, "Backend error. r:Retry"); break;
    case EntryState::Pending:
      if (pending == 0) {
        std::snprintf(out, n, "No requests pending");
      } else {
        std::snprintf(out, n, "%u pending", static_cast<unsigned>(pending));
      }
      break;
  }
}

bool summary_is_blank(const char* summary) {
  if (summary == nullptr) return true;
  for (; *summary != '\0'; ++summary) {
    if (*summary != ' ') return false;
  }
  return true;
}

void format_approval_row(const Approval& a, uint32_t remaining_s, char* out, size_t n) {
  if (n == 0) return;
  char right[8];
  std::snprintf(right, sizeof(right), "%us", static_cast<unsigned>(remaining_s > 999 ? 999 : remaining_s));
  const size_t left_w = static_cast<size_t>(kRowCols) - 1 - std::strlen(right);
  char text[kToolCap + kSummaryCap + 16];
  std::snprintf(text, sizeof(text), "%s %s", a.tool, summary_is_blank(a.summary) ? "(no details)" : a.summary);
  char left[kRowCols + 1];
  clip(text, left_w, left);
  std::snprintf(out, n, "%-*s %s", static_cast<int>(left_w), left, right);
}

// ---- screen text: confirm page -----------------------------------------------------------------------------

void format_confirm_operation(const char* tool, char* out, size_t n) {
  if (n == 0) return;
  char full[kToolCap + 16];
  std::snprintf(full, sizeof(full), "Claude Code: %s", tool_name(tool));
  clip(full, n - 1, out);
}

void format_confirm_target(char* out, size_t n) { std::snprintf(out, n, "Details shortened"); }

namespace {

// The word wrap of ui::wrap_next (src/ui/widgets.cpp): bytes consumed by the next row, 0 when exhausted. Keep
// both in step, the confirm hint below depends on how many rows the summary takes.
size_t wrap_consumed(const char* s, int cols) {
  if (s == nullptr || *s == '\0') return 0;
  int n = 0;
  int last_space = -1;
  while (s[n] != '\0' && s[n] != '\n' && n < cols) {
    if (s[n] == ' ') last_space = n;
    ++n;
  }
  size_t take = static_cast<size_t>(n);
  size_t consumed = take;
  if (s[n] == '\n') {
    consumed = take + 1;
  } else if (s[n] != '\0' && s[n] != ' ' && last_space > 0) {
    take = static_cast<size_t>(last_space);
    consumed = take + 1;
  } else if (s[n] == ' ') {
    consumed = take + 1;
  }
  return consumed;
}

}  // namespace

uint8_t wrap_rows(const char* s, int cols) {
  uint8_t rows = 0;
  while (s != nullptr && rows < 250) {
    const size_t used = wrap_consumed(s, cols);
    if (used == 0) break;
    s += used;
    ++rows;
  }
  return rows;
}

void format_confirm_effect(const char* summary, char* out, size_t n) {
  Text t(out, n);
  const bool blank = summary_is_blank(summary);
  const char* shown = blank ? "(no details)" : summary;
  uint8_t rows = wrap_rows(shown, kRowCols);
  if (rows <= kConfirmEffectRows) {
    t.add(shown);  // the widget's own word wrap shows all of it
  } else {
    // Word wrap would need a fourth row (the widget would end the third with '~'): cut at the row width instead,
    // so every character of the summary (at most 80) stays on screen.
    const size_t len = std::strlen(shown);
    char chunk[kRowCols + 1];
    for (size_t at = 0; at < len; at += kRowCols) {
      if (at > 0) t.add("\n");
      const size_t take = len - at < static_cast<size_t>(kRowCols) ? len - at : static_cast<size_t>(kRowCols);
      std::memcpy(chunk, shown + at, take);
      chunk[take] = '\0';
      t.add(chunk);
    }
    rows = static_cast<uint8_t>((len + kRowCols - 1) / kRowCols);
  }
  // The summary comes first and is never pushed out by the hint: a hint that would need a fourth row is dropped
  // (odd spacing in the summary can add a row that the count above does not show).
  const size_t base = t.size();
  if (rows <= 1) {
    t.add("\nAllow once. Deny what you cannot judge.");
  } else if (rows == 2) {
    t.add("\nDeny if unsure.");
  }
  if (n > base && wrap_rows(out, kRowCols) > kConfirmEffectRows) out[base] = '\0';
}

// ---- screen text: usage line -----------------------------------------------------------------------------

void format_cost(float usd, char* out, size_t n) {
  double v = static_cast<double>(usd);
  if (!(v >= 0.0)) v = 0.0;
  if (v > 1000000.0) v = 1000000.0;
  if (v < 0.99995) {
    std::snprintf(out, n, "$0.%04u", static_cast<unsigned>(v * 10000.0 + 0.5));
  } else {
    const unsigned long cents = static_cast<unsigned long>(v * 100.0 + 0.5);
    std::snprintf(out, n, "$%lu.%02lu", cents / 100, cents % 100);
  }
}

void format_percent(float pct, char* out, size_t n) {
  double v = static_cast<double>(pct);
  if (!(v >= 0.0)) v = 0.0;
  if (v > 100.0) v = 100.0;
  const unsigned tenths = static_cast<unsigned>(v * 10.0 + 0.5);
  if (tenths % 10 == 0) {
    std::snprintf(out, n, "%u%%", tenths / 10);
  } else {
    std::snprintf(out, n, "%u.%u%%", tenths / 10, tenths % 10);
  }
}

void format_age(uint32_t seconds, char* out, size_t n) {
  if (seconds < 60) {
    std::snprintf(out, n, "%u s ago", static_cast<unsigned>(seconds));
  } else if (seconds < 3600) {
    std::snprintf(out, n, "%u min ago", static_cast<unsigned>(seconds / 60));
  } else if (seconds < 86400) {
    std::snprintf(out, n, "%u h ago", static_cast<unsigned>(seconds / 3600));
  } else {
    std::snprintf(out, n, "%u d ago", static_cast<unsigned>(seconds / 86400));
  }
}

namespace {

// Appends " piece" to `line` (capacity kRowCols + 1) when it still fits.
void add_piece(char* line, const char* piece) {
  const size_t have = std::strlen(line);
  const size_t add = std::strlen(piece);
  if (have + 1 + add > static_cast<size_t>(kRowCols)) return;
  line[have] = ' ';
  std::memcpy(line + have + 1, piece, add + 1);
}

}  // namespace

void format_usage_line(const Status& s, char* out, size_t n) {
  if (n == 0) return;
  if (!s.configured) {
    std::snprintf(out, n, "Usage: no status yet");
    return;
  }
  char line[kRowCols + 1];
  char model[11];
  clip(s.model[0] != '\0' ? s.model : "?", sizeof(model) - 1, model);
  std::snprintf(line, sizeof(line), "Usage %s", model);
  char piece[24];
  char value[16];
  if (s.has_cost) {
    format_cost(s.cost_usd, piece, sizeof(piece));
    add_piece(line, piece);
  }
  if (s.has_context) {
    format_percent(s.context_pct, value, sizeof(value));
    std::snprintf(piece, sizeof(piece), "ctx %s", value);
    add_piece(line, piece);
  }
  if (s.has_five_hour) {
    format_percent(s.five_hour_pct, value, sizeof(value));
    std::snprintf(piece, sizeof(piece), "5h %s", value);
    add_piece(line, piece);
  }
  if (s.has_seven_day) {
    format_percent(s.seven_day_pct, value, sizeof(value));
    std::snprintf(piece, sizeof(piece), "7d %s", value);
    add_piece(line, piece);
  }
  std::snprintf(out, n, "%s", line);
}

void format_status_body(const Status& s, uint32_t age_s, char* out, size_t n) {
  Text t(out, n);
  if (!s.configured) {
    t.add("No Claude Code status received yet (see docs/CLAUDE_CODE_LINK.md).");
    return;
  }
  char value[20];
  t.addf("Model: %s", s.model[0] != '\0' ? s.model : "?");
  if (s.has_cost) {
    format_cost(s.cost_usd, value, sizeof(value));
    t.newline();
    t.addf("Cost (estimate): %s", value);
  }
  if (s.has_context) {
    format_percent(s.context_pct, value, sizeof(value));
    t.newline();
    t.addf("Context used: %s", value);
  }
  if (s.has_five_hour) {
    format_percent(s.five_hour_pct, value, sizeof(value));
    t.newline();
    t.addf("5-hour plan used: %s", value);
  }
  if (s.has_seven_day) {
    format_percent(s.seven_day_pct, value, sizeof(value));
    t.newline();
    t.addf("7-day plan used: %s", value);
  }
  if (s.has_age) {
    format_age(age_s, value, sizeof(value));
    t.newline();
    t.addf("Updated %s.", value);
  }
  t.newline();
  t.add("As reported by Claude Code. The cost is its estimate at list price.");
}

// ---- screen text: results ------------------------------------------------------------------------------------

namespace {

void set_title(char* title, size_t tn, const char* text) { std::snprintf(title, tn, "%s", text); }

// Seconds named by "retry in N s" in an error detail (the backend's 429 text); 0 when absent.
int retry_seconds(const char* detail) {
  const char* p = std::strstr(detail, "retry in ");
  if (p == nullptr) return 0;
  p += 9;
  int v = 0;
  int digits = 0;
  while (*p >= '0' && *p <= '9' && digits < 4) {
    v = v * 10 + (*p - '0');
    ++p;
    ++digits;
  }
  return digits > 0 ? v : 0;
}

}  // namespace

void format_answer_ok(bool allow, const char* tool, char* title, size_t tn, char* body, size_t bn) {
  const char* name = tool_name(tool);
  if (allow) {
    set_title(title, tn, "Allowed");
    std::snprintf(body, bn,
                  "Claude Code: %s\nAllowed once. The answer was recorded on the backend.\nClaude Code may still ask or "
                  "refuse by its own rules.",
                  name);
  } else {
    set_title(title, tn, "Denied");
    std::snprintf(body, bn, "Claude Code: %s\nDenied. The answer was recorded on the backend.", name);
  }
}

FailureKind classify_failure(int status, const deck::ApiError& err) {
  if (status == -1 || status == -2 || status == -6 || status == kStatusNoMemory || status == kStatusBuildFailed) {
    return FailureKind::NotSent;  // the request never left the device
  }
  if (status <= 0 || status == 200) return FailureKind::Unknown;  // sent, but no usable answer came back
  switch (status) {
    case 401:
    case 403: return FailureKind::Rejected;
    case 404: return FailureKind::Gone;
    case 409:
      if (std::strcmp(err.code, "expired") == 0) return FailureKind::TooLate;
      if (std::strcmp(err.code, "already_decided") == 0) return FailureKind::AlreadyAnswered;
      return FailureKind::Other;
    case 429: return FailureKind::RateLimited;
    default: return FailureKind::Other;
  }
}

void format_failure(FailureKind k, int status, const deck::ApiError& err, const char* tool, char* title, size_t tn,
                    char* body, size_t bn) {
  const char* name = tool_name(tool);
  Text t(body, bn);
  t.addf("Claude Code: %s\n", name);
  switch (k) {
    case FailureKind::NotSent:
      set_title(title, tn, "!Not sent");
      if (status == -1) {
        t.add("Backend offline or not set up. Your answer was NOT sent.");
      } else if (status == -2) {
        t.add("The radio is busy. Your answer was NOT sent.");
      } else if (status == -6) {
        t.add("The device clock is not set. Your answer was NOT sent.");
      } else if (status == kStatusNoMemory) {
        t.add("Out of memory on the device. Your answer was NOT sent.");
      } else {
        t.add("Your answer was NOT sent.");
      }
      t.add(" Open the list again.");
      break;
    case FailureKind::Unknown:
      set_title(title, tn, "!Status unknown");
      t.add("No usable answer. Your answer may or may not have been recorded. It is NOT sent again; open the list "
            "to see.");
      break;
    case FailureKind::TooLate:
      set_title(title, tn, "!Too late");
      t.add("Too late: it timed out. Answer it at the PC.");
      break;
    case FailureKind::AlreadyAnswered:
      set_title(title, tn, "!Already answered");
      t.add("Already answered, possibly by an earlier press whose reply was lost. Not sent again.");
      break;
    case FailureKind::Rejected:
      set_title(title, tn, "!Rejected");
      if (status == 403) {
        t.add("The backend rejected the answer: the code for this request is stale. Open the list again.");
      } else {
        t.add("The backend rejected this device. Enrol it again.");
      }
      break;
    case FailureKind::Gone:
      set_title(title, tn, "!No longer there");
      t.add("That request no longer exists. Open the list again.");
      break;
    case FailureKind::RateLimited: {
      set_title(title, tn, "!Rate limit");
      const int wait = retry_seconds(err.detail);
      if (wait > 0) {
        t.addf("Too many answers. Wait %d s, then try again. Not sent again by itself.", wait);
      } else {
        t.add("Too many answers. Wait a moment, then try again. Not sent again by itself.");
      }
      break;
    }
    case FailureKind::Other:
      set_title(title, tn, "!Not done");
      t.addf("The backend answered HTTP %d. The answer may not have been recorded. It is NOT sent again.", status);
      break;
  }
}

void format_expired(const char* tool, char* title, size_t tn, char* body, size_t bn) {
  set_title(title, tn, "!Expired");
  std::snprintf(body, bn, "Claude Code: %s\nIt timed out before you answered. Answer it at the PC.",
                tool_name(tool));
}

}  // namespace oyster::claude

