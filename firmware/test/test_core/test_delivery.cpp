// Result delivery rules (delivery_policy.h) and the NTP floor rule (clock_policy.h).
#include <unity.h>

#include <cstdio>
#include <cstring>

#include "oyster_core/clock_policy.h"
#include "oyster_core/delivery_policy.h"
#include "oyster_core/ids.h"
#include "oyster_core/upload_ledger.h"
#include "test_common.h"

using namespace oyster;

namespace {

// Fate by sequence number: <= accepted_through accepted (minus skipped), then pending up to stored_through,
// everything else lost.
class FakeFates : public EventFateSource {
 public:
  uint32_t accepted_through = 0;
  uint32_t stored_through = 0;
  uint32_t skipped_seq = 0;
  bool foreign = false;
  EventFate fate(uint32_t seq) const override {
    if (foreign) return EventFate::ForeignSession;
    if (seq <= accepted_through) return seq == skipped_seq ? EventFate::Skipped : EventFate::Accepted;
    if (seq <= stored_through) return EventFate::Pending;
    return EventFate::Lost;
  }
};

}  // namespace

static void test_parse_event_sequence() {
  uint32_t v = 0;
  TEST_ASSERT_TRUE(parse_event_sequence("evt-000012", v));
  TEST_ASSERT_EQUAL_UINT32(12, v);
  TEST_ASSERT_TRUE(parse_event_sequence("evt-123456", v));
  TEST_ASSERT_EQUAL_UINT32(123456, v);
  TEST_ASSERT_TRUE(parse_event_sequence("evt-1234567", v));  // more than 6 digits when needed
  TEST_ASSERT_EQUAL_UINT32(1234567, v);
  char id[cfg::kIdBufLen];
  TEST_ASSERT_TRUE(format_event_id(4294967295u, id, sizeof(id)));
  TEST_ASSERT_TRUE(parse_event_sequence(id, v));
  TEST_ASSERT_EQUAL_UINT32(4294967295u, v);

  v = 77;
  const char* bad[] = {nullptr,          "",           "evt-",         "evt-12",       "evt-0000012", "evt-000000",
                       "evt--00012",     "evt-00001x", "EVT-000012",   "event-000012", "evt-000012 ", " evt-000012",
                       "evt-4294967296", "evt-99999999999", "res-000012"};
  for (const char* b : bad) {
    TEST_ASSERT_FALSE_MESSAGE(parse_event_sequence(b, v), b != nullptr ? b : "(null)");
    TEST_ASSERT_EQUAL_UINT32(77, v);  // untouched on failure
  }
}

static void test_session_citable() {
  TEST_ASSERT_TRUE(session_citable("sess-aaaa-001", "sess-aaaa-001"));
  TEST_ASSERT_FALSE(session_citable("sess-aaaa-001", "sess-aaaa-002"));
  TEST_ASSERT_FALSE(session_citable("sess-aaaa-001", ""));      // no session open
  TEST_ASSERT_FALSE(session_citable("", ""));                   // unparsable job: session unknown
  TEST_ASSERT_FALSE(session_citable("", "sess-aaaa-001"));
  TEST_ASSERT_FALSE(session_citable(nullptr, "sess-aaaa-001"));
  TEST_ASSERT_FALSE(session_citable("sess-aaaa-001", nullptr));
}

static void test_plan_cites_only_accepted_events() {
  FakeFates f;
  f.accepted_through = 5;
  f.stored_through = 8;
  f.skipped_seq = 3;  // refused permanently and skipped
  const char* ids[] = {"evt-000001", "evt-000003", "evt-000005", "evt-000007", "evt-000020", "", "garbage"};
  const CitationPlan p = plan_citations(ids, 7, f);
  TEST_ASSERT_EQUAL_UINT8(6, p.total);  // the empty id is not counted
  TEST_ASSERT_EQUAL_UINT8(2, p.citable);
  TEST_ASSERT_EQUAL_STRING("evt-000001", p.ids[0]);
  TEST_ASSERT_EQUAL_STRING("evt-000005", p.ids[1]);
  TEST_ASSERT_EQUAL_UINT8(1, p.skipped);
  TEST_ASSERT_EQUAL_UINT8(1, p.pending);
  TEST_ASSERT_EQUAL_UINT8(1, p.lost);
  TEST_ASSERT_EQUAL_UINT8(0, p.foreign);
  TEST_ASSERT_EQUAL_UINT8(1, p.malformed);
  TEST_ASSERT_EQUAL_UINT32(3, p.not_citable(false));
  TEST_ASSERT_EQUAL_UINT32(4, p.not_citable(true));  // giving up on the pending one
}

static void test_plan_other_session_cites_nothing() {
  FakeFates f;
  f.accepted_through = 100;
  f.stored_through = 100;
  f.foreign = true;  // the job's session is not the open one: same-numbered events must never be cited
  const char* ids[] = {"evt-000001", "evt-000002"};
  const CitationPlan p = plan_citations(ids, 2, f);
  TEST_ASSERT_EQUAL_UINT8(0, p.citable);
  TEST_ASSERT_EQUAL_UINT8(2, p.foreign);
  char text[200];
  describe_uncited(p, false, text, sizeof(text));
  TEST_ASSERT_TRUE(std::strncmp(text, "session_not_active:", 19) == 0);
}

static void test_plan_is_bounded_and_ordered() {
  FakeFates f;
  f.accepted_through = 1000;
  f.stored_through = 1000;
  char store[40][cfg::kIdBufLen];
  const char* ids[40];
  for (uint32_t i = 0; i < 40; ++i) {
    format_event_id(i + 1, store[i], cfg::kIdBufLen);
    ids[i] = store[i];
  }
  const CitationPlan p = plan_citations(ids, 40, f);
  TEST_ASSERT_EQUAL_UINT8(cfg::kMaxResultEventIds, p.citable);
  TEST_ASSERT_EQUAL_STRING("evt-000001", p.ids[0]);
  TEST_ASSERT_EQUAL_STRING("evt-000016", p.ids[15]);
  const CitationPlan none = plan_citations(nullptr, 3, f);
  TEST_ASSERT_EQUAL_UINT8(0, none.total);
}

static void test_describe_uncited() {
  FakeFates f;
  f.accepted_through = 5;
  f.stored_through = 8;
  f.skipped_seq = 3;
  char text[200];
  const char* all_ok[] = {"evt-000001", "evt-000002"};
  describe_uncited(plan_citations(all_ok, 2, f), false, text, sizeof(text));
  TEST_ASSERT_EQUAL_STRING("", text);

  const char* mixed[] = {"evt-000001", "evt-000003", "evt-000007", "evt-000020"};
  const CitationPlan p = plan_citations(mixed, 4, f);
  describe_uncited(p, false, text, sizeof(text));
  TEST_ASSERT_EQUAL_STRING(
      "evidence_dropped: 2 of 4 cited events were not accepted by the backend (skipped 1, lost 1, other session 0, "
      "not uploaded 0)",
      text);
  describe_uncited(p, true, text, sizeof(text));
  TEST_ASSERT_TRUE(std::strstr(text, "3 of 4") != nullptr);
  TEST_ASSERT_TRUE(std::strstr(text, "not uploaded 1") != nullptr);

  char tiny[12];
  describe_uncited(p, false, tiny, sizeof(tiny));  // truncates, stays terminated
  TEST_ASSERT_EQUAL_UINT32(11, std::strlen(tiny));
}

static void test_compose_result_error() {
  char out[64];
  compose_result_error("", "", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("", out);
  compose_result_error("base text", "", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("base text", out);
  compose_result_error("", "evidence_dropped: 1 of 2", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("evidence_dropped: 1 of 2", out);
  compose_result_error("base text", "evidence_dropped: 1 of 2", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("evidence_dropped: 1 of 2; base text", out);
  compose_result_error("base text that is long", "evidence_dropped: 1 of 2", out, 30);  // note survives truncation
  TEST_ASSERT_EQUAL_UINT32(29, std::strlen(out));
  TEST_ASSERT_TRUE(std::strncmp(out, "evidence_dropped: 1 of 2; ", 26) == 0);
  compose_result_error("a", "b", nullptr, 10);  // no crash
  compose_result_error("a", "b", out, 0);
}

static void test_classify_submit_failure() {
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Transport), static_cast<int>(classify_submit_failure(-1, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Transport), static_cast<int>(classify_submit_failure(0, nullptr)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Auth), static_cast<int>(classify_submit_failure(401, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Auth), static_cast<int>(classify_submit_failure(403, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::ServerError), static_cast<int>(classify_submit_failure(500, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::ServerError), static_cast<int>(classify_submit_failure(503, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::ServerError), static_cast<int>(classify_submit_failure(408, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::ServerError), static_cast<int>(classify_submit_failure(429, "")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::EventsMissing),
                        static_cast<int>(classify_submit_failure(409, "events_missing")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Refused), static_cast<int>(classify_submit_failure(409, "conflict")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Refused), static_cast<int>(classify_submit_failure(409, nullptr)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Refused), static_cast<int>(classify_submit_failure(422, "validation_failed")));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(SubmitFailure::Refused), static_cast<int>(classify_submit_failure(404, "not_found")));
}

// An undeliverable result (409 events_missing every time) must be abandoned after a bounded number of
// attempts so job polling resumes.
static void test_events_missing_is_abandoned_after_five_attempts() {
  ResultRetryState st;
  uint32_t age = 0;
  uint32_t total_delay = 0;
  int attempts = 0;
  RetryDecision d{};
  do {
    ++attempts;
    d = decide_result_retry(st, SubmitFailure::EventsMissing, age);
    if (!d.abandon) {
      age += d.delay_ms;
      total_delay += d.delay_ms;
    }
  } while (!d.abandon && attempts < 50);
  TEST_ASSERT_TRUE(d.abandon);
  TEST_ASSERT_EQUAL_INT(kMaxRefusedResultAttempts, attempts);
  TEST_ASSERT_EQUAL_UINT32(2000 + 4000 + 8000 + 16000, total_delay);  // backoff doubles
  TEST_ASSERT_TRUE(std::strlen(d.reason) > 0);
  TEST_ASSERT_TRUE(total_delay < kResultMaxAgeMs);
}

static void test_refused_is_abandoned_after_five_attempts_and_backoff_is_capped() {
  ResultRetryState st;
  RetryDecision d{};
  for (int i = 1; i <= 4; ++i) {
    d = decide_result_retry(st, SubmitFailure::Refused, 1000);
    TEST_ASSERT_FALSE(d.abandon);
  }
  d = decide_result_retry(st, SubmitFailure::Refused, 1000);
  TEST_ASSERT_TRUE(d.abandon);

  // Transient failures never exhaust the attempt budget, but their backoff is capped.
  ResultRetryState t;
  for (int i = 0; i < 40; ++i) {
    d = decide_result_retry(t, SubmitFailure::ServerError, 5000);
    TEST_ASSERT_FALSE(d.abandon);
    TEST_ASSERT_TRUE(d.delay_ms <= kResultRetryMaxMs);
  }
  TEST_ASSERT_EQUAL_UINT32(kResultRetryMaxMs, d.delay_ms);
  TEST_ASSERT_EQUAL_UINT8(0, t.refused_attempts);
}

static void test_every_failure_kind_is_abandoned_at_the_age_limit() {
  const SubmitFailure kinds[] = {SubmitFailure::Transport, SubmitFailure::Auth, SubmitFailure::ServerError,
                                 SubmitFailure::EventsMissing, SubmitFailure::Refused};
  for (SubmitFailure k : kinds) {
    ResultRetryState st;
    TEST_ASSERT_FALSE(decide_result_retry(st, k, kResultMaxAgeMs - 1).abandon);
    ResultRetryState st2;
    const RetryDecision d = decide_result_retry(st2, k, kResultMaxAgeMs);
    TEST_ASSERT_TRUE(d.abandon);
    TEST_ASSERT_TRUE(std::strlen(d.reason) > 0);
  }
  TEST_ASSERT_TRUE(result_may_wait(kResultMaxAgeMs - 1));
  TEST_ASSERT_FALSE(result_may_wait(kResultMaxAgeMs));
}

static JobSpec make_job(uint64_t received) {
  JobSpec j{};
  std::strcpy(j.job_id, "job-1");
  std::strcpy(j.nonce, "nonce-1");
  j.received_ms = received;
  return j;
}

static LocalConfirmation make_conf(uint64_t decided, bool approved = true) {
  LocalConfirmation c{};
  std::strcpy(c.job_id, "job-1");
  std::strcpy(c.nonce, "nonce-1");
  c.approved = approved;
  c.decided_ms = decided;
  return c;
}

static void test_confirmation_freshness_at_execution() {
  const uint32_t timeout = 120000;
  const JobSpec job = make_job(10000);
  // Approved 5 s after receipt, executed 60 s later: fresh.
  TEST_ASSERT_TRUE(confirmation_fresh_for_execution(job, make_conf(15000), 75000, timeout));
  // Exactly at the age limit: still fresh; one ms later: stale (the approval waited too long).
  TEST_ASSERT_TRUE(confirmation_fresh_for_execution(job, make_conf(15000), 15000 + timeout, timeout));
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, make_conf(15000), 15000 + timeout + 1, timeout));
  // The approval was valid when given but the start was delayed by retries past the window.
  TEST_ASSERT_TRUE(confirmation_valid(job, make_conf(15000), timeout));
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, make_conf(15000), 400000, timeout));
  // Not approved, wrong job, wrong nonce, decided before the job existed, clock going backwards.
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, make_conf(15000, false), 20000, timeout));
  LocalConfirmation wrong_job = make_conf(15000);
  std::strcpy(wrong_job.job_id, "job-2");
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, wrong_job, 20000, timeout));
  LocalConfirmation wrong_nonce = make_conf(15000);
  std::strcpy(wrong_nonce.nonce, "other");
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, wrong_nonce, 20000, timeout));
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, make_conf(9000), 20000, timeout));
  TEST_ASSERT_FALSE(confirmation_fresh_for_execution(job, make_conf(15000), 14999, timeout));
}

static void test_ntp_floor_rule() {
  const uint64_t floor = 1767225600ULL;            // 2026-01-01
  const uint64_t day = 86400ULL;
  TEST_ASSERT_TRUE(ntp_floor_may_advance(floor + 1, floor));
  TEST_ASSERT_TRUE(ntp_floor_may_advance(floor + 300 * day, floor));
  TEST_ASSERT_TRUE(ntp_floor_may_advance(floor + kMaxNtpFloorAdvanceSeconds, floor));         // boundary: 400 days
  TEST_ASSERT_FALSE(ntp_floor_may_advance(floor + kMaxNtpFloorAdvanceSeconds + 1, floor));
  TEST_ASSERT_FALSE(ntp_floor_may_advance(4000000000ULL, floor));                              // spoofed far future (2096)
  TEST_ASSERT_FALSE(ntp_floor_may_advance(floor, floor));                                      // no advance
  TEST_ASSERT_FALSE(ntp_floor_may_advance(floor - 1, floor));                                  // never moves back
  TEST_ASSERT_FALSE(ntp_floor_may_advance(0, floor));
  // A persisted far-future floor cannot be raised further either, but a normal step from a normal floor works.
  const uint64_t seed = floor + 200 * day;
  TEST_ASSERT_TRUE(ntp_floor_may_advance(seed + 30 * day, seed));
  TEST_ASSERT_FALSE(ntp_floor_may_advance(seed + 401 * day, seed));
  TEST_ASSERT_EQUAL_UINT32(400u * 86400u, static_cast<uint32_t>(kMaxNtpFloorAdvanceSeconds));
}

// ---- UploadLedger: what the backend accepted, skipped or never received -----------------------------

static void test_ledger_contiguous_accept() {
  UploadLedger l;
  TEST_ASSERT_TRUE(l.accept(1, 5, 10));
  TEST_ASSERT_EQUAL_UINT32(5, l.acked());
  TEST_ASSERT_EQUAL_UINT32(5, l.resolved());
  TEST_ASSERT_EQUAL_UINT32(0, l.skipped());
  TEST_ASSERT_EQUAL_UINT32(0, l.hole_count());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(3, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Pending), static_cast<int>(l.fate(6, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(11, 10)));  // never written
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(0, 10)));
  // Stale or invalid acknowledgements change nothing.
  TEST_ASSERT_FALSE(l.accept(1, 5, 10));
  TEST_ASSERT_FALSE(l.accept(0, 3, 10));
  TEST_ASSERT_FALSE(l.accept(7, 6, 10));
  TEST_ASSERT_EQUAL_UINT32(5, l.resolved());
  // An acknowledgement cannot reach past what was written.
  TEST_ASSERT_TRUE(l.accept(6, 99, 10));
  TEST_ASSERT_EQUAL_UINT32(10, l.acked());
}

static void test_ledger_skipped_event_is_never_acked() {
  UploadLedger l;
  TEST_ASSERT_TRUE(l.accept(1, 3, 12));
  TEST_ASSERT_TRUE(l.skip(4, 12));                 // refused for good
  TEST_ASSERT_EQUAL_UINT32(3, l.acked());          // acked does not move over it
  TEST_ASSERT_EQUAL_UINT32(4, l.resolved());       // but upload does
  TEST_ASSERT_EQUAL_UINT32(1, l.skipped());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Skipped), static_cast<int>(l.fate(4, 12)));
  TEST_ASSERT_TRUE(l.accept(5, 9, 12));
  TEST_ASSERT_EQUAL_UINT32(9, l.acked());          // later events are acked ...
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Skipped), static_cast<int>(l.fate(4, 12)));  // ... 4 stays skipped
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(5, 12)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(3, 12)));
  TEST_ASSERT_EQUAL_UINT32(1, l.skipped());
  // A skipped event at the very end: the cursor moves, acked stays below it.
  TEST_ASSERT_TRUE(l.skip(10, 12));
  TEST_ASSERT_EQUAL_UINT32(9, l.acked());
  TEST_ASSERT_EQUAL_UINT32(10, l.resolved());
  TEST_ASSERT_EQUAL_UINT32(2, l.skipped());
  // Cannot skip what is already resolved, was never written or is 0.
  TEST_ASSERT_FALSE(l.skip(10, 12));
  TEST_ASSERT_FALSE(l.skip(9, 12));
  TEST_ASSERT_FALSE(l.skip(13, 12));
  TEST_ASSERT_FALSE(l.skip(0, 12));
  TEST_ASSERT_EQUAL_UINT32(2, l.skipped());
}

static void test_ledger_gap_before_a_batch_is_lost_not_accepted() {
  // No SD card: events 3..5 were overwritten in the RAM ring before they could be uploaded.
  UploadLedger l;
  TEST_ASSERT_TRUE(l.accept(1, 2, 10));
  TEST_ASSERT_TRUE(l.accept(6, 8, 10));
  TEST_ASSERT_EQUAL_UINT32(8, l.acked());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(3, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(5, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(2, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(6, 10)));
  TEST_ASSERT_EQUAL_UINT32(0, l.skipped());  // lost is not skipped: dropped_events() counts it elsewhere

  // A refused event after a gap records both.
  UploadLedger m;
  TEST_ASSERT_TRUE(m.accept(1, 3, 10));
  TEST_ASSERT_TRUE(m.skip(7, 10));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(m.fate(5, 10)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Skipped), static_cast<int>(m.fate(7, 10)));
  TEST_ASSERT_EQUAL_UINT32(1, m.skipped());
}

static void test_ledger_adjacent_holes_merge_and_old_holes_become_unknown() {
  UploadLedger l;
  TEST_ASSERT_TRUE(l.accept(1, 3, 100));
  TEST_ASSERT_TRUE(l.skip(4, 100));
  TEST_ASSERT_TRUE(l.skip(5, 100));
  TEST_ASSERT_EQUAL_UINT32(1, l.hole_count());  // 4..5 refused, one range
  TEST_ASSERT_EQUAL_UINT32(4, l.hole(0).first);
  TEST_ASSERT_EQUAL_UINT32(5, l.hole(0).last);
  TEST_ASSERT_EQUAL_UINT32(2, l.skipped());

  // Skip every other event far past the hole capacity: counters stay exact, memory stays bounded and
  // sequences older than the oldest remembered hole are Lost, never Accepted.
  UploadLedger m;
  for (uint32_t s = 2; s <= 80; s += 2) TEST_ASSERT_TRUE(m.skip(s, 100));
  TEST_ASSERT_EQUAL_UINT32(40, m.skipped());
  TEST_ASSERT_TRUE(m.hole_count() <= UploadLedger::kMaxHoles);
  TEST_ASSERT_TRUE(m.hole_floor() > 0);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(m.fate(1, 100)));   // old: unknown
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Skipped), static_cast<int>(m.fate(80, 100)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(m.fate(79, 100)));  // never sent hole
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Pending), static_cast<int>(m.fate(81, 100)));
}

static void test_ledger_restore_and_reset() {
  UploadLedger l;
  l.restore(5, 7, 2);  // a resumed session: only the counters survive
  TEST_ASSERT_EQUAL_UINT32(5, l.acked());
  TEST_ASSERT_EQUAL_UINT32(7, l.resolved());
  TEST_ASSERT_EQUAL_UINT32(2, l.skipped());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(3, 20)));    // unknown history
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Lost), static_cast<int>(l.fate(7, 20)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Pending), static_cast<int>(l.fate(8, 20)));
  TEST_ASSERT_TRUE(l.accept(8, 9, 20));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(EventFate::Accepted), static_cast<int>(l.fate(8, 20)));
  l.restore(9, 4, 0);  // resolved can never be below acked
  TEST_ASSERT_EQUAL_UINT32(9, l.resolved());
  l.reset();
  TEST_ASSERT_EQUAL_UINT32(0, l.acked());
  TEST_ASSERT_EQUAL_UINT32(0, l.resolved());
  TEST_ASSERT_EQUAL_UINT32(0, l.skipped());
  TEST_ASSERT_EQUAL_UINT32(0, l.hole_count());
  TEST_ASSERT_EQUAL_UINT32(0, l.hole_floor());
}

// The cases that need the ledger: a refused event and an overwritten one are never cited, the ones the
// backend accepted are, and what is still uploading makes the result wait.
class LedgerFates : public EventFateSource {
 public:
  UploadLedger ledger;
  uint32_t written = 0;
  uint32_t stored_through = 0;
  EventFate fate(uint32_t seq) const override {
    const EventFate f = ledger.fate(seq, written);
    if (f != EventFate::Pending) return f;
    return seq <= stored_through ? EventFate::Pending : EventFate::Lost;
  }
};

static void test_citations_over_a_ledger() {
  LedgerFates f;
  f.written = 12;
  f.stored_through = 12;
  f.ledger.accept(1, 3, 12);
  f.ledger.skip(4, 12);        // refused single event
  f.ledger.accept(7, 9, 12);   // 5..6 were overwritten before upload
  const char* ids[] = {"evt-000002", "evt-000004", "evt-000006", "evt-000008", "evt-000011"};
  const CitationPlan p = plan_citations(ids, 5, f);
  TEST_ASSERT_EQUAL_UINT8(2, p.citable);
  TEST_ASSERT_EQUAL_STRING("evt-000002", p.ids[0]);
  TEST_ASSERT_EQUAL_STRING("evt-000008", p.ids[1]);
  TEST_ASSERT_EQUAL_UINT8(1, p.skipped);
  TEST_ASSERT_EQUAL_UINT8(1, p.lost);
  TEST_ASSERT_EQUAL_UINT8(1, p.pending);  // 11 is stored but not uploaded yet: the result waits

  // The ring overwrote it while it was unresolved: it can never be uploaded, so it is Lost, not Pending.
  f.stored_through = 10;
  TEST_ASSERT_EQUAL_UINT8(0, plan_citations(ids, 5, f).pending);
  TEST_ASSERT_EQUAL_UINT8(2, plan_citations(ids, 5, f).lost);
}

void run_delivery_tests() {
  RUN_TEST(test_parse_event_sequence);
  RUN_TEST(test_session_citable);
  RUN_TEST(test_plan_cites_only_accepted_events);
  RUN_TEST(test_plan_other_session_cites_nothing);
  RUN_TEST(test_plan_is_bounded_and_ordered);
  RUN_TEST(test_describe_uncited);
  RUN_TEST(test_compose_result_error);
  RUN_TEST(test_classify_submit_failure);
  RUN_TEST(test_events_missing_is_abandoned_after_five_attempts);
  RUN_TEST(test_refused_is_abandoned_after_five_attempts_and_backoff_is_capped);
  RUN_TEST(test_every_failure_kind_is_abandoned_at_the_age_limit);
  RUN_TEST(test_confirmation_freshness_at_execution);
  RUN_TEST(test_ntp_floor_rule);
  RUN_TEST(test_ledger_contiguous_accept);
  RUN_TEST(test_ledger_skipped_event_is_never_acked);
  RUN_TEST(test_ledger_gap_before_a_batch_is_lost_not_accepted);
  RUN_TEST(test_ledger_adjacent_holes_merge_and_old_holes_become_unknown);
  RUN_TEST(test_ledger_restore_and_reset);
  RUN_TEST(test_citations_over_a_ledger);
}
