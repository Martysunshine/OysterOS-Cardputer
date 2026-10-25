// Pure decisions for delivering a job result (host-tested; no I/O).
//
// Invariant: a result cites only event ids that (1) were written to the JOB's own session and (2) the
// backend accepted. The backend checks cited ids against the job's session, so an id from another session
// (or one the backend never accepted) either makes the result undeliverable (409 events_missing) or cites
// a different event with the same number. A result that cannot cite evidence says so in `error`
// ("session_not_active", "evidence_dropped") instead of citing it.
//
// An undeliverable result never blocks remote jobs forever: refusals are retried a bounded number of
// times with backoff, and every result is abandoned (with a local event) after kResultMaxAgeMs.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/local_policy.h"
#include "oyster_core/types.h"

namespace oyster {

// ---- which event ids a result may cite ------------------------------------------------------------------

enum class EventFate : uint8_t {
  Accepted,        // the backend accepted it: citable
  Pending,         // stored on the device, not yet resolved: may still be uploaded
  Skipped,         // the backend refused it permanently; kept locally, never acknowledged
  Lost,            // no longer stored (volatile ring overwrote it) or never stored
  ForeignSession,  // belongs to a session other than the job's
};
const char* to_string(EventFate f);

// "evt-000012" -> 12. False for anything else (no sign, at least 6 digits, at most 10 digits, fits uint32).
bool parse_event_sequence(const char* event_id, uint32_t& sequence);

// The job's session may be cited only while it is the session the evidence store has open.
bool session_citable(const char* job_session_id, const char* open_session_id);

// Fate lookup for one session, supplied by the evidence store (device) or a fake (tests).
class EventFateSource {
 public:
  virtual ~EventFateSource() = default;
  virtual EventFate fate(uint32_t sequence) const = 0;
};

struct CitationPlan {
  char ids[cfg::kMaxResultEventIds][cfg::kIdBufLen];  // citable ids, original order
  uint8_t citable = 0;
  uint8_t pending = 0;
  uint8_t skipped = 0;
  uint8_t lost = 0;
  uint8_t foreign = 0;
  uint8_t malformed = 0;  // not an event id (empty, wrong shape)
  uint8_t total = 0;      // non-empty ids examined (at most kMaxResultEventIds)

  // Ids that will not be cited; with give_up_pending, ids still pending count too.
  size_t not_citable(bool give_up_pending) const {
    return static_cast<size_t>(skipped) + lost + foreign + malformed + (give_up_pending ? pending : 0u);
  }
};

CitationPlan plan_citations(const char* const* ids, size_t n, const EventFateSource& src);

// Writes the explanation for uncited ids, or "" when everything is citable.
// "session_not_active: 3 of 3 cited events belong to a session that is no longer open" /
// "evidence_dropped: 2 of 5 cited events were not accepted by the backend (skipped 1, lost 1, not uploaded 0)".
void describe_uncited(const CitationPlan& plan, bool give_up_pending, char* out, size_t n);

// base + "; " + note, truncated to fit n (NUL included). Either part may be empty.
void compose_result_error(const char* base, const char* note, char* out, size_t n);

// ---- retry and abandonment of an undeliverable result --------------------------------------------------

enum class SubmitFailure : uint8_t {
  Transport,      // no HTTP response (offline, timeout): retried until the age limit
  Auth,           // 401/403: retried until the age limit
  ServerError,    // 5xx, 408, 429: retried until the age limit
  EventsMissing,  // 409 events_missing after the upload-and-retry already done by the client
  Refused,        // other 4xx: the same request will not be accepted
};
SubmitFailure classify_submit_failure(int http_status, const char* error_code);

constexpr uint8_t kMaxRefusedResultAttempts = 5;
constexpr uint32_t kResultRetryMinMs = 2000;
constexpr uint32_t kResultRetryMaxMs = 60000;
constexpr uint32_t kResultMaxAgeMs = 30u * 60u * 1000u;

struct ResultRetryState {
  uint8_t refused_attempts = 0;  // EventsMissing + Refused failures so far
  uint32_t backoff_ms = 0;
};

struct RetryDecision {
  bool abandon;
  uint32_t delay_ms;     // wait before the next attempt (when !abandon)
  const char* reason;    // static text when abandoning, "" otherwise
};

// Updates `st` for one failed submission. age_ms = time since the result was queued.
RetryDecision decide_result_retry(ResultRetryState& st, SubmitFailure failure, uint32_t age_ms);

// True while a result still waiting for evidence to be uploaded may wait (age limit not reached).
inline bool result_may_wait(uint32_t age_ms) { return age_ms < kResultMaxAgeMs; }

// ---- confirmation freshness ------------------------------------------------------------------------------

// The approval must be valid for this exact job (confirmation_valid) AND no older than timeout_ms at the
// moment the operation is about to run; an approval that waited too long is not used.
bool confirmation_fresh_for_execution(const JobSpec& job, const LocalConfirmation& c, uint64_t now_ms,
                                      uint32_t timeout_ms);

}  // namespace oyster
