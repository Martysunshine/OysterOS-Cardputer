// Control Deck wire format on the device (host tested): bounded structs, strict parsers for the backend's
// replies (docs/CONTROL_DECK.md) and the text shown on the 240x135 screen. No Arduino headers, no heap use of
// its own (ArduinoJson allocates while a parse runs and releases before it returns).
//
// Parsing rules (the reply is untrusted input):
//  - ids, names and slot labels must fit their buffer exactly and use the allowed characters, otherwise the
//    whole reply is rejected (nothing is truncated into a different identifier);
//  - free text for the screen (reasons, summaries) is cut to its buffer and any byte outside printable ASCII
//    becomes '?';
//  - counts are bounded (profiles <= 5, slots <= 9, steps <= 3); a larger array rejects the reply;
//  - a rejected parse leaves `out` zeroed, so a half-filled struct is never used.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::deck {

constexpr uint8_t kMaxProfiles = 5;  // one device page (the backend caps a device page at 5)
constexpr uint8_t kMaxSlots = 9;
constexpr uint8_t kMaxSteps = 3;
constexpr uint8_t kMaxHistory = 10;

constexpr size_t kIdCap = 41;        // profile id, host id: 1..40 characters
constexpr size_t kLabelCap = 25;     // profile name, slot label: 1..24 printable ASCII
constexpr size_t kTokenCap = 65;     // job id, nonce: 1..64 (a nonce is 8..128 on the wire, see kNonceCap)
constexpr size_t kNonceCap = 129;
constexpr size_t kTypeCap = 24;      // action type name
constexpr size_t kServiceCap = 65;   // service name: 1..64
constexpr size_t kCodeCap = 25;      // reason / step code (cut when longer)
constexpr size_t kSummaryCap = 64;   // step summary (the backend allows 120; the screen needs less)
constexpr size_t kProfileShortCap = 17;

// ---- profiles --------------------------------------------------------------------------------------------

struct Slot {
  uint8_t number;  // 1..9
  bool impactful;  // needs a confirmation on the device (the row shows '*')
  char label[kLabelCap];
};

struct Profile {
  char id[kIdCap];
  char name[kLabelCap];
  uint8_t slot_count;
  Slot slots[kMaxSlots];  // ascending by number
};

struct ProfilePage {
  Profile profiles[kMaxProfiles];
  uint8_t count;
  char next_after[kIdCap];  // cursor of the next page, empty when this is the last
};

// GET /v1/deck/profiles?limit=5[&after=ID] -> {"profiles":[{profile_id,name,slots:[{slot,label,impactful}]}],"next_after"}
bool parse_profiles(const char* json, size_t len, ProfilePage& out);

const Slot* find_slot(const Profile& p, uint8_t number);  // nullptr when the profile has no such slot

// ---- jobs ------------------------------------------------------------------------------------------------

enum class JobState : uint8_t {
  Unrecognised,  // a state this firmware does not know: shown as such, never polled forever
  Queued,
  AwaitingConfirmation,
  Running,
  Succeeded,
  Failed,
  Cancelled,
  Expired,
  UnknownOutcome,
};

enum class StepOutcome : uint8_t { Unrecognised, Pending, Succeeded, Failed, Unknown, Skipped };

struct Step {
  char type[kTypeCap];
  char host[kIdCap];  // empty for actions without a host (noop)
  char service[kServiceCap];
  StepOutcome outcome;
  char code[kCodeCap];
  char summary[kSummaryCap];
};

struct Job {
  char id[kTokenCap];
  char profile_id[kIdCap];
  uint8_t slot;
  JobState state;
  bool requires_confirmation;
  bool outcome_known;          // false only for UnknownOutcome (the action may have run)
  char executor[12];           // "mock" | "windows" | ...
  char reason[kCodeCap];       // empty when none
  char nonce[kNonceCap];       // only while AwaitingConfirmation, only for the owning device; never displayed
  uint32_t confirm_window_s;   // confirm_deadline - created_at (backend clock), 0 when absent
  uint8_t step_count;
  Step steps[kMaxSteps];
};

// A job reply: GET /v1/deck/jobs/{id}, POST .../run, .../confirm, .../cancel.
bool parse_job(const char* json, size_t len, Job& out);

bool job_is_terminal(JobState s);  // Succeeded, Failed, Cancelled, Expired, UnknownOutcome, Unrecognised
bool executor_is_mock(const Job& j);

// ---- history ---------------------------------------------------------------------------------------------

enum class HistOutcome : uint8_t { Other, Succeeded, Failed, Cancelled, Expired, UnknownOutcome, Denied };

struct HistoryEntry {
  char profile[kProfileShortCap];  // cut for display
  uint8_t slot;                    // 0 when absent
  HistOutcome outcome;
  char job_id[kTokenCap];          // empty for a denied request without a job
};

struct HistoryPage {
  HistoryEntry entries[kMaxHistory];
  uint8_t count;  // finished entries only: "pending" rows (queued, running, awaiting) are dropped
};

// GET /v1/deck/history?limit=10 -> {"entries":[{event,outcome,profile_id,slot,job_id,...}],"next_before"}
bool parse_history(const char* json, size_t len, HistoryPage& out);

// ---- errors ----------------------------------------------------------------------------------------------

struct ApiError {
  char code[kCodeCap];      // "expired", "rate_limited", ...
  char detail[kSummaryCap];
};
// Error body {"error": code, "detail": text}; an unparsable body leaves both empty.
void parse_api_error(const char* json, size_t len, ApiError& out);

// Text for a finished request that did not produce a job. `status` is the HTTP status or the negative
// transport code of modules::http (-1 offline, -3 i/o, -4 protocol, -5 too large, -6 clock not set).
void format_error(int status, const ApiError& err, char* out, size_t n);
// True when the request may have reached the backend although no usable answer came back (i/o, protocol,
// oversized reply): the caller keeps the Idempotency-Key and offers a status check instead of a new run.
bool status_may_have_reached_backend(int status);

// ---- request building --------------------------------------------------------------------------------------

bool valid_name(const char* s, size_t cap);  // 1..cap-1 characters of [A-Za-z0-9_-]: safe in a path and a JSON body
bool path_profiles(char* out, size_t n, const char* after);       // after may be nullptr or ""
bool path_run(char* out, size_t n, const char* profile_id, uint8_t slot);
bool path_job(char* out, size_t n, const char* job_id);
bool path_confirm(char* out, size_t n, const char* job_id);
bool path_cancel(char* out, size_t n, const char* job_id);
bool path_history(char* out, size_t n, uint8_t limit);
bool body_confirm(char* out, size_t n, const char* job_id, const char* nonce, bool approved);
// "dk-" + 24 hex digits from three words: a fresh key per user action (8..80 characters on the wire).
void format_idempotency_key(char* out, size_t n, uint32_t a, uint32_t b, uint32_t c);

// ---- screen text -------------------------------------------------------------------------------------------

bool is_impactful_type(const char* action_type);  // lock_workstation, wake_on_lan, service_restart
void format_profile_row(const Profile& p, char* out, size_t n);
void format_slot_row(const Slot& s, char* out, size_t n);
void format_history_row(const HistoryEntry& e, char* out, size_t n);
void format_job_title(const Job& j, char* out, size_t n);  // <= 31 characters, leading '!' = alert
// Body of the result page. `label` is the slot label ("" when unknown: profile/slot is shown instead).
void format_job_body(const Job& j, const char* label, char* out, size_t n);
void format_confirm_target(const Job& j, char* out, size_t n);  // "this PC" or the host id
void format_confirm_effect(const Job& j, char* out, size_t n);  // what the impactful steps do
// "N profile(s)" for the Home tile; `more` adds '+' (the first page is full and another follows).
void format_tile_count(uint8_t count, bool more, char* out, size_t n);

}  // namespace oyster::deck
