// Claude Code link on the device (host tested): bounded structs, strict parsers for the backend's replies
// (docs/CLAUDE_CODE_LINK.md, "Contract for the firmware screens") and the text shown on the 240x135 screen.
// No Arduino headers, no heap use of its own (ArduinoJson allocates while a parse runs and releases before it
// returns). Error bodies are read with oyster::deck::parse_api_error.
//
// Parsing rules (the reply is untrusted input, same as deck_json.h):
//  - approval ids, tool names and nonces must fit their buffer exactly and use the allowed characters,
//    otherwise the whole reply is rejected (nothing is truncated into a different identifier);
//  - the summary is display text: cut to its buffer, any byte outside printable ASCII becomes '?';
//  - more than kMaxApprovals entries, a missing nonce or a missing expiry rejects the reply;
//  - a rejected parse leaves `out` zeroed, so a half-filled struct is never used.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/deck_json.h"

namespace oyster::claude {

constexpr uint8_t kMaxApprovals = 5;  // what one device list returns
constexpr size_t kIdCap = 41;         // approval_id: 1..40 characters of [A-Za-z0-9_-] (21 on the wire today)
constexpr size_t kToolCap = 41;       // tool: 1..40 characters of [A-Za-z0-9_.:-]
constexpr size_t kSummaryCap = 81;    // summary: 0..80 printable ASCII characters
constexpr size_t kNonceCap = 33;      // nonce: 8..32 characters of [A-Za-z0-9_-] (22 on the wire today)
constexpr size_t kModelCap = 41;      // model name of the usage line
constexpr uint32_t kMaxExpiresS = 600;  // the window is at most 120 s; a larger value is clamped, never trusted
constexpr int kRowCols = 28;          // list row width of the 240x135 screen (ui::kTextCols)
constexpr int kConfirmEffectRows = 3;  // rows the confirm widget has for the effect text
constexpr const char* kPathApprovals = "/v1/claude/approvals";
constexpr const char* kPathStatus = "/v1/claude/status";
// Codes the module uses besides the negative ones of modules::http: nothing was sent in either case.
constexpr int kStatusNoMemory = -7;
constexpr int kStatusBuildFailed = -8;

// ---- approvals -------------------------------------------------------------------------------------------

struct Approval {
  char id[kIdCap];
  char tool[kToolCap];
  char summary[kSummaryCap];  // empty when the backend sent none
  char nonce[kNonceCap];      // made for THIS device and approval; RAM only, never shown or logged
  uint32_t age_s;
  uint32_t expires_in_s;      // at the moment the backend answered
};

struct ApprovalPage {
  Approval items[kMaxApprovals];  // newest first, as the backend sends them
  uint8_t count;
};

// GET /v1/claude/approvals -> {"approvals":[{approval_id,tool,summary,age_s,expires_in_s,nonce}]}
bool parse_approvals(const char* json, size_t len, ApprovalPage& out);

// ---- usage line ------------------------------------------------------------------------------------------

struct Status {
  bool configured;   // false: the backend never received a status line
  bool has_age;
  uint32_t updated_s_ago;
  char model[kModelCap];
  bool has_cost;
  float cost_usd;    // Claude Code's own estimate at list price
  bool has_context;
  float context_pct;
  bool has_five_hour;
  float five_hour_pct;
  bool has_seven_day;
  float seven_day_pct;
};

// GET /v1/claude/status -> {"configured":false} or the usage line. Percentages outside 0..100 and a cost
// outside 0..1,000,000 reject the reply; null or absent fields are simply not present.
bool parse_status(const char* json, size_t len, Status& out);

// ---- decision --------------------------------------------------------------------------------------------

enum class Decided : uint8_t { None, Allowed, Denied };
struct DecideReply {
  Decided state;                 // what the backend recorded
  char approval_id[kIdCap];      // echo, empty when absent
};
// POST /v1/claude/approvals/{id}/decide -> {"approval_id","state":"allowed"|"denied","decision"}
bool parse_decide_reply(const char* json, size_t len, DecideReply& out);

// ---- request building ------------------------------------------------------------------------------------

bool path_decide(char* out, size_t n, const char* approval_id);
// {"decision":"allow"|"deny","nonce":"..."}; the nonce must be a valid token (8..32 of [A-Za-z0-9_-]).
bool body_decide(char* out, size_t n, bool allow, const char* nonce);

// ---- time ------------------------------------------------------------------------------------------------

// Deadline helpers on a wrapping millisecond clock (the same arithmetic as millis()).
bool deadline_passed(uint32_t now_ms, uint32_t deadline_ms);
uint32_t seconds_left(uint32_t now_ms, uint32_t deadline_ms);  // rounded up, 0 when passed
uint32_t deadline_from(uint32_t sent_ms, uint32_t expires_in_s);  // clamps expires_in_s to kMaxExpiresS

// ---- screen text -----------------------------------------------------------------------------------------

// What the first page shows for the Claude Code row and the approvals page shows when there is no list.
enum class EntryState : uint8_t { Checking, Pending, Offline, NotSetUp, NotAccepted, Error };
// `status` is the HTTP status or the negative transport code of modules::http for a failed fetch.
EntryState entry_state_for_failure(int status);
void format_entry_row(EntryState st, uint8_t pending, char* out, size_t n);  // "Claude Code  2 pending", <= 28
void format_list_note(EntryState st, uint8_t pending, char* out, size_t n);  // approvals page, no rows

bool summary_is_blank(const char* summary);  // empty or only spaces: shown as "(no details)"
// One approvals row: "<tool> <summary>" clipped, then the remaining time right-aligned. At most kRowCols
// characters, out must hold kRowCols + 1.
void format_approval_row(const Approval& a, uint32_t remaining_s, char* out, size_t n);

// Confirm page fields (ui::draw_confirm shows operation and target on one row each and wraps the effect over
// kConfirmEffectRows rows, so the summary goes to the effect: it is the part the owner must be able to read).
void format_confirm_operation(const char* tool, char* out, size_t n);  // "Claude Code: Bash" (<= 39)
void format_confirm_target(char* out, size_t n);                       // "Details shortened"
void format_confirm_effect(const char* summary, char* out, size_t n);  // summary, then a hint when rows remain
// Rows the widget needs for `s` at `cols` columns (the same word wrap as ui::wrap_next).
uint8_t wrap_rows(const char* s, int cols);

// One-line usage status for a list row: "Usage Opus $0.0123 ctx 8%" (pieces that do not fit are left out).
void format_usage_line(const Status& s, char* out, size_t n);
// Text page: every field the backend sent; the two plan percentages only when present. `age_s` is
// updated_s_ago plus the time since the reply arrived.
void format_status_body(const Status& s, uint32_t age_s, char* out, size_t n);
void format_cost(float usd, char* out, size_t n);       // "$0.0123" below one dollar, "$12.34" above
void format_percent(float pct, char* out, size_t n);    // "8%" or "12.5%"
void format_age(uint32_t seconds, char* out, size_t n);  // "7 s ago", "3 min ago", "2 h ago"

// Result texts. Titles are <= 31 characters; a leading '!' is an alert heading.
void format_answer_ok(bool allow, const char* tool, char* title, size_t tn, char* body, size_t bn);
enum class FailureKind : uint8_t { NotSent, Unknown, TooLate, AlreadyAnswered, Rejected, Gone, RateLimited, Other };
FailureKind classify_failure(int status, const deck::ApiError& err);
// `status` as for entry_state_for_failure; the nonce is never part of any text.
void format_failure(FailureKind k, int status, const deck::ApiError& err, const char* tool, char* title, size_t tn,
                    char* body, size_t bn);
void format_expired(const char* tool, char* title, size_t tn, char* body, size_t bn);

}  // namespace oyster::claude
