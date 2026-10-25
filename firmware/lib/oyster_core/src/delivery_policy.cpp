// Pure delivery decisions for job results (see delivery_policy.h).
#include "oyster_core/delivery_policy.h"

#include <cstdio>
#include <cstring>

#include "oyster_core/ids.h"

namespace oyster {

const char* to_string(EventFate f) {
  switch (f) {
    case EventFate::Accepted: return "accepted";
    case EventFate::Pending: return "pending";
    case EventFate::Skipped: return "skipped";
    case EventFate::Lost: return "lost";
    case EventFate::ForeignSession: return "foreign_session";
  }
  return "lost";
}

bool parse_event_sequence(const char* event_id, uint32_t& sequence) {
  if (event_id == nullptr || std::strncmp(event_id, "evt-", 4) != 0) return false;
  const char* d = event_id + 4;
  size_t digits = 0;
  uint64_t v = 0;
  for (; d[digits] != '\0'; ++digits) {
    if (d[digits] < '0' || d[digits] > '9' || digits >= 10) return false;
    v = v * 10 + static_cast<uint64_t>(d[digits] - '0');
  }
  if (digits == 0 || v == 0 || v > UINT32_MAX) return false;
  // Only the canonical spelling written by format_event_id() is an event id of this firmware.
  char canon[cfg::kIdBufLen];
  if (!format_event_id(static_cast<uint32_t>(v), canon, sizeof(canon)) || std::strcmp(canon, event_id) != 0) return false;
  sequence = static_cast<uint32_t>(v);
  return true;
}

bool session_citable(const char* job_session_id, const char* open_session_id) {
  if (job_session_id == nullptr || open_session_id == nullptr) return false;
  if (job_session_id[0] == '\0' || open_session_id[0] == '\0') return false;
  return std::strncmp(job_session_id, open_session_id, cfg::kIdBufLen) == 0;
}

CitationPlan plan_citations(const char* const* ids, size_t n, const EventFateSource& src) {
  CitationPlan p;
  std::memset(p.ids, 0, sizeof(p.ids));
  for (size_t i = 0; ids != nullptr && i < n && p.total < cfg::kMaxResultEventIds; ++i) {
    if (ids[i] == nullptr || ids[i][0] == '\0') continue;
    ++p.total;
    uint32_t seq = 0;
    if (!parse_event_sequence(ids[i], seq)) {
      ++p.malformed;
      continue;
    }
    switch (src.fate(seq)) {
      case EventFate::Accepted:
        copy_bounded(p.ids[p.citable], cfg::kIdBufLen, ids[i]);
        ++p.citable;
        break;
      case EventFate::Pending: ++p.pending; break;
      case EventFate::Skipped: ++p.skipped; break;
      case EventFate::Lost: ++p.lost; break;
      case EventFate::ForeignSession: ++p.foreign; break;
    }
  }
  return p;
}

void describe_uncited(const CitationPlan& plan, bool give_up_pending, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  out[0] = '\0';
  const size_t gone = plan.not_citable(give_up_pending);
  if (gone == 0) return;
  if (plan.foreign > 0 && plan.citable == 0) {
    std::snprintf(out, n, "session_not_active: %u of %u cited events belong to a session that is no longer open",
                  static_cast<unsigned>(plan.foreign), static_cast<unsigned>(plan.total));
    return;
  }
  std::snprintf(out, n,
                "evidence_dropped: %u of %u cited events were not accepted by the backend (skipped %u, lost %u, "
                "other session %u, not uploaded %u)",
                static_cast<unsigned>(gone), static_cast<unsigned>(plan.total), static_cast<unsigned>(plan.skipped),
                static_cast<unsigned>(plan.lost), static_cast<unsigned>(plan.foreign),
                static_cast<unsigned>(give_up_pending ? plan.pending : 0u));
}

void compose_result_error(const char* base, const char* note, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  out[0] = '\0';
  const bool has_base = base != nullptr && base[0] != '\0';
  const bool has_note = note != nullptr && note[0] != '\0';
  if (has_note && has_base) {
    std::snprintf(out, n, "%s; %s", note, base);  // the evidence note first: it must survive truncation
  } else if (has_note) {
    std::snprintf(out, n, "%s", note);
  } else if (has_base) {
    std::snprintf(out, n, "%s", base);
  }
}

SubmitFailure classify_submit_failure(int http_status, const char* error_code) {
  if (http_status <= 0) return SubmitFailure::Transport;
  if (http_status == 401 || http_status == 403) return SubmitFailure::Auth;
  if (http_status == 408 || http_status == 429 || http_status >= 500) return SubmitFailure::ServerError;
  if (http_status == 409 && error_code != nullptr && std::strcmp(error_code, "events_missing") == 0) {
    return SubmitFailure::EventsMissing;
  }
  if (http_status >= 400) return SubmitFailure::Refused;
  return SubmitFailure::ServerError;  // a 2xx/3xx is not a failure; if reported anyway, retry conservatively
}

RetryDecision decide_result_retry(ResultRetryState& st, SubmitFailure failure, uint32_t age_ms) {
  if (age_ms >= kResultMaxAgeMs) return RetryDecision{true, 0, "result not delivered within 30 minutes"};
  if (failure == SubmitFailure::EventsMissing || failure == SubmitFailure::Refused) {
    if (st.refused_attempts < UINT8_MAX) ++st.refused_attempts;
    if (st.refused_attempts >= kMaxRefusedResultAttempts) {
      return RetryDecision{true, 0,
                           failure == SubmitFailure::EventsMissing ? "backend kept answering events_missing"
                                                                   : "backend refused the result repeatedly"};
    }
  }
  if (st.backoff_ms == 0) {
    st.backoff_ms = kResultRetryMinMs;
  } else {
    st.backoff_ms = st.backoff_ms * 2 > kResultRetryMaxMs ? kResultRetryMaxMs : st.backoff_ms * 2;
  }
  return RetryDecision{false, st.backoff_ms, ""};
}

bool confirmation_fresh_for_execution(const JobSpec& job, const LocalConfirmation& c, uint64_t now_ms,
                                      uint32_t timeout_ms) {
  if (!confirmation_valid(job, c, timeout_ms)) return false;
  return now_ms >= c.decided_ms && now_ms - c.decided_ms <= timeout_ms;
}

}  // namespace oyster
