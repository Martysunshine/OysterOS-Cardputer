// Control Deck wire format: strict bounded parsers and the screen text. See deck_json.h for the rules.
#include "oyster_core/deck_json.h"

#include <ArduinoJson.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <initializer_list>

#include "oyster_core/json_writer.h"

namespace oyster::deck {

namespace {

constexpr int kNestingLimit = 8;
constexpr size_t kMaxHistoryEntriesOnWire = 50;

// Identifiers that reach a URL path or a JSON body: [A-Za-z0-9_-]. No '.', '/', '?', '%' or space.
bool is_id_char(char c) {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
}

// Service names may also hold '.', '@' (display only, never placed in a path).
bool is_service_char(char c) { return is_id_char(c) || c == '.' || c == '@'; }

bool is_printable(char c) { return c >= 0x20 && c <= 0x7E; }

template <typename T>
bool reject(T& out) {
  std::memset(&out, 0, sizeof(out));
  return false;
}

// A string that must fit `cap` exactly and use only identifier characters (ids, tokens, action types, hosts);
// `service` also allows '.' and '@'.
bool read_name(JsonVariantConst v, char* out, size_t cap, bool allow_empty = false, bool service = false) {
  out[0] = '\0';
  if (!v.is<const char*>()) return false;
  const char* s = v.as<const char*>();
  const size_t n = std::strlen(s);
  if (n == 0) return allow_empty;
  if (n >= cap) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!(service ? is_service_char(s[i]) : is_id_char(s[i]))) return false;
  }
  std::memcpy(out, s, n + 1);
  return true;
}

// A label: 1..cap-1 printable ASCII characters, exactly.
bool read_label(JsonVariantConst v, char* out, size_t cap) {
  out[0] = '\0';
  if (!v.is<const char*>()) return false;
  const char* s = v.as<const char*>();
  const size_t n = std::strlen(s);
  if (n == 0 || n >= cap) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!is_printable(s[i])) return false;
  }
  std::memcpy(out, s, n + 1);
  return true;
}

// Copy that cuts at dst's size (display only).
void copy_cut(char* dst, size_t cap, const char* src) {
  size_t i = 0;
  for (; src[i] != '\0' && i + 1 < cap; ++i) dst[i] = src[i];
  dst[i] = '\0';
}

// Display text: absent or null is empty; anything else is cut to the buffer, non-printable bytes become '?'.
void read_display(JsonVariantConst v, char* out, size_t cap) {
  out[0] = '\0';
  if (!v.is<const char*>()) return;
  const char* s = v.as<const char*>();
  size_t i = 0;
  for (; s[i] != '\0' && i + 1 < cap; ++i) out[i] = is_printable(s[i]) ? s[i] : '?';
  out[i] = '\0';
}

bool parse_doc(JsonDocument& doc, const JsonDocument& filter, const char* json, size_t len) {
  if (json == nullptr || len == 0) return false;
  const DeserializationError e = deserializeJson(doc, json, len, DeserializationOption::Filter(filter),
                                                 DeserializationOption::NestingLimit(kNestingLimit));
  return e == DeserializationError::Ok && doc.is<JsonObject>();
}

JobState job_state_from(const char* s) {
  struct Map {
    const char* name;
    JobState state;
  };
  static const Map kMap[] = {
      {"queued", JobState::Queued},       {"awaiting_confirmation", JobState::AwaitingConfirmation},
      {"running", JobState::Running},     {"succeeded", JobState::Succeeded},
      {"failed", JobState::Failed},       {"cancelled", JobState::Cancelled},
      {"expired", JobState::Expired},     {"unknown_outcome", JobState::UnknownOutcome},
  };
  for (const Map& m : kMap) {
    if (std::strcmp(s, m.name) == 0) return m.state;
  }
  return JobState::Unrecognised;
}

StepOutcome step_outcome_from(const char* s) {
  if (std::strcmp(s, "pending") == 0) return StepOutcome::Pending;
  if (std::strcmp(s, "succeeded") == 0) return StepOutcome::Succeeded;
  if (std::strcmp(s, "failed") == 0) return StepOutcome::Failed;
  if (std::strcmp(s, "unknown") == 0) return StepOutcome::Unknown;
  if (std::strcmp(s, "skipped") == 0) return StepOutcome::Skipped;
  return StepOutcome::Unrecognised;
}

const char* step_outcome_name(StepOutcome o) {
  switch (o) {
    case StepOutcome::Pending: return "pending";
    case StepOutcome::Succeeded: return "succeeded";
    case StepOutcome::Failed: return "failed";
    case StepOutcome::Unknown: return "result unknown";
    case StepOutcome::Skipped: return "skipped";
    case StepOutcome::Unrecognised: break;
  }
  return "?";
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

const char* host_display(const char* host) { return std::strcmp(host, "this-pc") == 0 ? "this PC" : host; }

const char* reason_text(const char* code) {
  struct Map {
    const char* code;
    const char* text;
  };
  static const Map kMap[] = {
      {"declined", "rejected on the device"},
      {"cancelled_by_device", "cancelled on the device"},
      {"confirmation_timeout", "the confirmation window closed"},
      {"queue_timeout", "the backend did not start it in time"},
      {"run_timeout", "it ran past its time limit"},
      {"stalled", "the backend never started it"},
      {"queue_full", "the backend queue was full"},
      {"backend_restarted", "the backend restarted"},
      {"profile_changed", "the profile was changed"},
      {"profile_deleted", "the profile was deleted"},
      {"action_not_allowed", "the action is not allowed"},
      {"failed", "the action failed"},
      {"timeout", "the action timed out"},
      {"lost_result", "the result was lost"},
      {"executor_error", "the executor reported an error"},
      {"host_not_local", "refused: not this PC"},
      {"host_not_allowed", "refused: host not allowed"},
      {"service_not_allowed", "refused: service not allowed"},
      {"needs_admin", "needs administrator rights"},
  };
  for (const Map& m : kMap) {
    if (std::strcmp(code, m.code) == 0) return m.text;
  }
  return code;
}

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

const char* outcome_text(HistOutcome o) {
  switch (o) {
    case HistOutcome::Succeeded: return "succeeded";
    case HistOutcome::Failed: return "failed";
    case HistOutcome::Cancelled: return "cancelled";
    case HistOutcome::Expired: return "expired";
    case HistOutcome::UnknownOutcome: return "UNKNOWN";
    case HistOutcome::Denied: return "denied";
    case HistOutcome::Other: break;
  }
  return "other";
}

bool hist_outcome_from(const char* s, HistOutcome& out) {
  if (std::strcmp(s, "pending") == 0 || std::strcmp(s, "ok") == 0) return false;  // not a finished job
  if (std::strcmp(s, "succeeded") == 0) out = HistOutcome::Succeeded;
  else if (std::strcmp(s, "failed") == 0) out = HistOutcome::Failed;
  else if (std::strcmp(s, "cancelled") == 0) out = HistOutcome::Cancelled;
  else if (std::strcmp(s, "expired") == 0) out = HistOutcome::Expired;
  else if (std::strcmp(s, "unknown_outcome") == 0) out = HistOutcome::UnknownOutcome;
  else if (std::strcmp(s, "denied") == 0) out = HistOutcome::Denied;
  else out = HistOutcome::Other;
  return true;
}

}  // namespace

// ---- profiles ----------------------------------------------------------------------------------------------

bool parse_profiles(const char* json, size_t len, ProfilePage& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  filter["profiles"][0]["profile_id"] = true;
  filter["profiles"][0]["name"] = true;
  filter["profiles"][0]["slots"][0]["slot"] = true;
  filter["profiles"][0]["slots"][0]["label"] = true;
  filter["profiles"][0]["slots"][0]["impactful"] = true;
  filter["next_after"] = true;
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  JsonArrayConst list = doc["profiles"].as<JsonArrayConst>();
  if (list.isNull() || list.size() > kMaxProfiles) return reject(out);
  for (JsonObjectConst p : list) {
    Profile& dst = out.profiles[out.count];
    if (!read_name(p["profile_id"], dst.id, sizeof(dst.id)) || !read_label(p["name"], dst.name, sizeof(dst.name))) {
      return reject(out);
    }
    JsonArrayConst slots = p["slots"].as<JsonArrayConst>();
    if (slots.isNull() || slots.size() > kMaxSlots) return reject(out);
    for (JsonObjectConst s : slots) {
      JsonVariantConst num = s["slot"];
      JsonVariantConst imp = s["impactful"];
      if (!num.is<int>() || !imp.is<bool>()) return reject(out);
      const int n = num.as<int>();
      if (n < 1 || n > kMaxSlots || find_slot(dst, static_cast<uint8_t>(n)) != nullptr) return reject(out);
      Slot& sl = dst.slots[dst.slot_count];
      if (!read_label(s["label"], sl.label, sizeof(sl.label))) return reject(out);
      sl.number = static_cast<uint8_t>(n);
      sl.impactful = imp.as<bool>();
      ++dst.slot_count;
    }
    for (uint8_t i = 1; i < dst.slot_count; ++i) {  // ascending by slot number (insertion sort, <= 9 items)
      Slot cur = dst.slots[i];
      uint8_t j = i;
      while (j > 0 && dst.slots[j - 1].number > cur.number) {
        dst.slots[j] = dst.slots[j - 1];
        --j;
      }
      dst.slots[j] = cur;
    }
    ++out.count;
  }
  JsonVariantConst next = doc["next_after"];
  if (!next.isNull() && !read_name(next, out.next_after, sizeof(out.next_after))) return reject(out);
  return true;
}

const Slot* find_slot(const Profile& p, uint8_t number) {
  for (uint8_t i = 0; i < p.slot_count && i < kMaxSlots; ++i) {
    if (p.slots[i].number == number) return &p.slots[i];
  }
  return nullptr;
}

// ---- jobs ------------------------------------------------------------------------------------------------

bool parse_job(const char* json, size_t len, Job& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  for (const char* key : {"job_id", "profile_id", "slot", "state", "requires_confirmation", "executor", "created_at",
                          "confirm_deadline", "nonce", "reason", "outcome_known"}) {
    filter[key] = true;
  }
  for (const char* key : {"type", "host_id", "service", "outcome", "code", "summary"}) {
    filter["steps"][0][key] = true;
  }
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  if (!read_name(doc["job_id"], out.id, sizeof(out.id)) || !read_name(doc["profile_id"], out.profile_id, sizeof(out.profile_id))) {
    return reject(out);
  }
  JsonVariantConst slot = doc["slot"];
  JsonVariantConst state = doc["state"];
  JsonVariantConst needs = doc["requires_confirmation"];
  if (!slot.is<int>() || slot.as<int>() < 1 || slot.as<int>() > kMaxSlots || !state.is<const char*>() ||
      !needs.is<bool>()) {
    return reject(out);
  }
  out.slot = static_cast<uint8_t>(slot.as<int>());
  out.state = job_state_from(state.as<const char*>());
  out.requires_confirmation = needs.as<bool>();
  JsonVariantConst known = doc["outcome_known"];
  out.outcome_known = !(known.is<bool>() && !known.as<bool>());
  read_display(doc["executor"], out.executor, sizeof(out.executor));
  read_display(doc["reason"], out.reason, sizeof(out.reason));
  JsonVariantConst nonce = doc["nonce"];
  if (!nonce.isNull()) {
    if (!read_name(nonce, out.nonce, sizeof(out.nonce)) || std::strlen(out.nonce) < 8) return reject(out);
  }
  JsonVariantConst created = doc["created_at"];
  JsonVariantConst deadline = doc["confirm_deadline"];
  if (created.is<uint32_t>() && deadline.is<uint32_t>() && deadline.as<uint32_t>() > created.as<uint32_t>()) {
    out.confirm_window_s = deadline.as<uint32_t>() - created.as<uint32_t>();
  }
  JsonArrayConst steps = doc["steps"].as<JsonArrayConst>();
  if (steps.isNull() || steps.size() > kMaxSteps) return reject(out);
  for (JsonObjectConst s : steps) {
    Step& st = out.steps[out.step_count];
    if (!read_name(s["type"], st.type, sizeof(st.type))) return reject(out);
    if (!read_name(s["host_id"], st.host, sizeof(st.host), true) && !s["host_id"].isNull()) return reject(out);
    if (!read_name(s["service"], st.service, sizeof(st.service), true, true) && !s["service"].isNull()) {
      return reject(out);
    }
    JsonVariantConst outcome = s["outcome"];
    st.outcome = outcome.is<const char*>() ? step_outcome_from(outcome.as<const char*>()) : StepOutcome::Unrecognised;
    read_display(s["code"], st.code, sizeof(st.code));
    read_display(s["summary"], st.summary, sizeof(st.summary));
    ++out.step_count;
  }
  return true;
}

bool job_is_terminal(JobState s) {
  return s != JobState::Queued && s != JobState::AwaitingConfirmation && s != JobState::Running;
}

bool executor_is_mock(const Job& j) { return std::strcmp(j.executor, "mock") == 0; }

// ---- history ---------------------------------------------------------------------------------------------

bool parse_history(const char* json, size_t len, HistoryPage& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  for (const char* key : {"outcome", "profile_id", "slot", "job_id"}) filter["entries"][0][key] = true;
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return false;

  JsonArrayConst list = doc["entries"].as<JsonArrayConst>();
  if (list.isNull() || list.size() > kMaxHistoryEntriesOnWire) return reject(out);
  for (JsonObjectConst e : list) {
    JsonVariantConst outcome = e["outcome"];
    if (!outcome.is<const char*>()) return reject(out);
    HistOutcome o = HistOutcome::Other;
    if (!hist_outcome_from(outcome.as<const char*>(), o)) continue;  // queued / running / awaiting
    if (out.count >= kMaxHistory) continue;                          // newest first: the oldest are dropped
    HistoryEntry& dst = out.entries[out.count];
    dst.outcome = o;
    char profile[kIdCap];
    if (!read_name(e["profile_id"], profile, sizeof(profile), true) && !e["profile_id"].isNull()) return reject(out);
    copy_cut(dst.profile, sizeof(dst.profile), profile);
    JsonVariantConst slot = e["slot"];
    dst.slot = (slot.is<int>() && slot.as<int>() >= 1 && slot.as<int>() <= kMaxSlots) ? static_cast<uint8_t>(slot.as<int>()) : 0;
    if (!read_name(e["job_id"], dst.job_id, sizeof(dst.job_id), true) && !e["job_id"].isNull()) return reject(out);
    ++out.count;
  }
  return true;
}

// ---- errors ----------------------------------------------------------------------------------------------

void parse_api_error(const char* json, size_t len, ApiError& out) {
  std::memset(&out, 0, sizeof(out));
  JsonDocument filter;
  filter["error"] = true;
  filter["detail"] = true;
  JsonDocument doc;
  if (!parse_doc(doc, filter, json, len)) return;
  read_display(doc["error"], out.code, sizeof(out.code));
  read_display(doc["detail"], out.detail, sizeof(out.detail));
}

bool status_may_have_reached_backend(int status) { return status == -3 || status == -4 || status == -5; }

void format_error(int status, const ApiError& err, char* out, size_t n) {
  Text t(out, n);
  if (status < 0) {
    if (status == -1) {
      t.add("Backend offline or not set up. Nothing was sent.");
    } else if (status == -6) {
      t.add("The device clock is not set. Nothing was sent.");
    } else {
      t.add("No usable answer from the backend.");
    }
    return;
  }
  switch (status) {
    case 401:
    case 403:
      t.add("The backend rejected this device. Enrol it again.");
      return;
    case 404:
      t.add("Not found: the profile, slot or job no longer exists. Go back and reload.");
      return;
    case 409:
      if (std::strcmp(err.code, "expired") == 0) {
        t.add("The confirmation window closed. Nothing ran.");
      } else if (std::strcmp(err.code, "already_decided") == 0) {
        t.add("Already decided. Check History for the result.");
      } else if (std::strcmp(err.code, "not_cancellable") == 0) {
        t.add("It already started and cannot be cancelled.");
      } else if (std::strcmp(err.code, "conflict") == 0) {
        t.add("Request key conflict. Go back and try again.");
      } else if (std::strcmp(err.code, "action_not_allowed") == 0 || std::strcmp(err.code, "profile_invalid") == 0) {
        t.add("The profile no longer passes the backend's checks. Nothing ran.");
      } else {
        t.add("The backend refused it in its current state (HTTP 409).");
      }
      return;
    case 413:
    case 422:
      t.addf("The backend rejected the request (HTTP %d).", status);
      return;
    case 429: {
      const int wait = retry_seconds(err.detail);
      if (wait > 0) {
        t.addf("Too many requests. Wait %d s, then try again.", wait);
      } else {
        t.add("Too many requests. Wait a moment, then try again.");
      }
      return;
    }
    case 503:
      t.add("The backend is busy. Try again in a few seconds.");
      return;
    default:
      break;
  }
  if (status >= 500) {
    t.addf("Backend error (HTTP %d).", status);
  } else {
    t.addf("Unexpected reply (HTTP %d).", status);
  }
}

// ---- request building --------------------------------------------------------------------------------------

bool valid_name(const char* s, size_t cap) {
  if (s == nullptr) return false;
  size_t n = 0;
  for (; s[n] != '\0'; ++n) {
    if (n + 1 >= cap || !is_id_char(s[n])) return false;
  }
  return n > 0;
}

namespace {
bool fit(int written, size_t n) { return written > 0 && static_cast<size_t>(written) < n; }
}  // namespace

bool path_profiles(char* out, size_t n, const char* after) {
  if (after != nullptr && after[0] != '\0') {
    if (!valid_name(after, kIdCap)) return false;
    return fit(std::snprintf(out, n, "/v1/deck/profiles?limit=%u&after=%s", static_cast<unsigned>(kMaxProfiles), after), n);
  }
  return fit(std::snprintf(out, n, "/v1/deck/profiles?limit=%u", static_cast<unsigned>(kMaxProfiles)), n);
}

bool path_run(char* out, size_t n, const char* profile_id, uint8_t slot) {
  if (!valid_name(profile_id, kIdCap) || slot < 1 || slot > kMaxSlots) return false;
  return fit(std::snprintf(out, n, "/v1/deck/profiles/%s/slots/%u/run", profile_id, static_cast<unsigned>(slot)), n);
}

bool path_job(char* out, size_t n, const char* job_id) {
  if (!valid_name(job_id, kTokenCap)) return false;
  return fit(std::snprintf(out, n, "/v1/deck/jobs/%s", job_id), n);
}

bool path_confirm(char* out, size_t n, const char* job_id) {
  if (!valid_name(job_id, kTokenCap)) return false;
  return fit(std::snprintf(out, n, "/v1/deck/jobs/%s/confirm", job_id), n);
}

bool path_cancel(char* out, size_t n, const char* job_id) {
  if (!valid_name(job_id, kTokenCap)) return false;
  return fit(std::snprintf(out, n, "/v1/deck/jobs/%s/cancel", job_id), n);
}

bool path_history(char* out, size_t n, uint8_t limit) {
  if (limit < 1 || limit > 50) return false;
  return fit(std::snprintf(out, n, "/v1/deck/history?limit=%u", static_cast<unsigned>(limit)), n);
}

bool body_confirm(char* out, size_t n, const char* job_id, const char* nonce, bool approved) {
  if (!valid_name(job_id, kTokenCap) || !valid_name(nonce, kNonceCap)) return false;
  JsonWriter w(out, n);
  w.begin_object();
  w.str("job_id", job_id);
  w.str("nonce", nonce);
  w.boolean("approved", approved);
  w.end_object();
  return w.ok();
}

void format_idempotency_key(char* out, size_t n, uint32_t a, uint32_t b, uint32_t c) {
  std::snprintf(out, n, "dk-%08lx%08lx%08lx", static_cast<unsigned long>(a), static_cast<unsigned long>(b),
                static_cast<unsigned long>(c));
}

// ---- screen text -------------------------------------------------------------------------------------------

bool is_impactful_type(const char* t) {
  return t != nullptr && (std::strcmp(t, "lock_workstation") == 0 || std::strcmp(t, "wake_on_lan") == 0 ||
                          std::strcmp(t, "service_restart") == 0);
}

void format_profile_row(const Profile& p, char* out, size_t n) {
  std::snprintf(out, n, "%.16s (%u slots)", p.name, static_cast<unsigned>(p.slot_count));
}

void format_slot_row(const Slot& s, char* out, size_t n) {
  std::snprintf(out, n, "%u%c %s", static_cast<unsigned>(s.number), s.impactful ? '*' : ' ', s.label);
}

void format_history_row(const HistoryEntry& e, char* out, size_t n) {
  if (e.slot > 0) {
    std::snprintf(out, n, "%.12s/%u %s", e.profile, static_cast<unsigned>(e.slot), outcome_text(e.outcome));
  } else {
    std::snprintf(out, n, "%.12s %s", e.profile, outcome_text(e.outcome));
  }
}

void format_job_title(const Job& j, char* out, size_t n) {
  const char* t = "!Unknown job state";
  switch (j.state) {
    case JobState::Queued: t = "Queued"; break;
    case JobState::AwaitingConfirmation: t = "Waiting for approval"; break;
    case JobState::Running: t = "Running"; break;
    case JobState::Succeeded: t = "Job succeeded"; break;
    case JobState::Failed: t = "!Job failed"; break;
    case JobState::Cancelled: t = "Job cancelled"; break;
    case JobState::Expired: t = "!Job expired"; break;
    case JobState::UnknownOutcome: t = "!Outcome unknown"; break;
    case JobState::Unrecognised: break;
  }
  std::snprintf(out, n, "%s", t);
}

void format_job_body(const Job& j, const char* label, char* out, size_t n) {
  Text t(out, n);
  if (label != nullptr && label[0] != '\0') {
    t.add(label);
  } else {
    t.addf("%s/%u", j.profile_id, static_cast<unsigned>(j.slot));
  }
  t.newline();
  const bool by_device = std::strcmp(j.reason, "declined") == 0 || std::strcmp(j.reason, "cancelled_by_device") == 0;
  switch (j.state) {
    case JobState::Queued: t.add("Queued on the backend."); break;
    case JobState::AwaitingConfirmation: t.add("Waiting for your approval."); break;
    case JobState::Running: t.add("Running on the PC..."); break;
    case JobState::Succeeded: t.add("Finished: the executor reported success."); break;
    case JobState::Failed: t.add("Failed. Nothing was retried."); break;
    case JobState::Cancelled: t.add(by_device ? "Rejected on the device. Nothing was run." : "Cancelled before it ran."); break;
    case JobState::Expired: t.add("Expired before it ran. Nothing was run."); break;
    case JobState::UnknownOutcome:
      t.add("The action may have run. Check the PC before running it again. It is never retried automatically.");
      break;
    case JobState::Unrecognised: t.add("The backend reported a state this firmware does not know."); break;
  }
  for (uint8_t i = 0; i < j.step_count && i < kMaxSteps; ++i) {
    const Step& s = j.steps[i];
    t.newline();
    t.addf("%u %s: %s", static_cast<unsigned>(i + 1), s.type, step_outcome_name(s.outcome));
    if (s.summary[0] != '\0') {
      t.newline();
      t.addf("  %s", s.summary);
    }
  }
  if (j.reason[0] != '\0' && !by_device && j.state != JobState::Succeeded) {
    t.newline();
    t.addf("Reason: %s", reason_text(j.reason));
  }
  if (executor_is_mock(j)) {
    t.newline();
    t.add("Mock executor: nothing was run on the PC.");
  }
}

void format_confirm_target(const Job& j, char* out, size_t n) {
  for (uint8_t i = 0; i < j.step_count && i < kMaxSteps; ++i) {
    if (is_impactful_type(j.steps[i].type) && j.steps[i].host[0] != '\0') {
      std::snprintf(out, n, "%s", host_display(j.steps[i].host));
      return;
    }
  }
  std::snprintf(out, n, "Control Deck");
}

void format_confirm_effect(const Job& j, char* out, size_t n) {
  Text t(out, n);
  bool any_impactful = false;
  for (uint8_t i = 0; i < j.step_count && i < kMaxSteps; ++i) any_impactful |= is_impactful_type(j.steps[i].type);
  for (uint8_t i = 0; i < j.step_count && i < kMaxSteps; ++i) {
    const Step& s = j.steps[i];
    if (any_impactful && !is_impactful_type(s.type)) continue;  // read-only steps are not what is being approved
    if (t.size() > 0) t.add(" ");
    const char* host = s.host[0] != '\0' ? host_display(s.host) : "the host";
    if (std::strcmp(s.type, "lock_workstation") == 0) {
      t.addf("Locks the Windows session on %s.", host);
    } else if (std::strcmp(s.type, "wake_on_lan") == 0) {
      t.addf("Sends a wake-up packet for %s.", host);
    } else if (std::strcmp(s.type, "service_restart") == 0) {
      t.addf("Restarts service %s on %s.", s.service, host);
    } else {
      t.addf("Runs %s on %s.", s.type, host);
    }
  }
}

void format_tile_count(uint8_t count, bool more, char* out, size_t n) {
  if (count == 0) {
    std::snprintf(out, n, "no profile");
  } else if (count == 1 && !more) {
    std::snprintf(out, n, "1 profile");
  } else {
    std::snprintf(out, n, "%u%s profiles", static_cast<unsigned>(count), more ? "+" : "");
  }
}

}  // namespace oyster::deck
