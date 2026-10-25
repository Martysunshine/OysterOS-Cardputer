// AssessmentRunner: the single worker task (queue depth 4). Each loop: one UI request -> (uplink
// allowed) upload evidence, retry pending results, poll one job -> LocalPolicy -> local confirmation
// when the operation type needs it -> status "running" accepted by the backend (HTTP 200; otherwise
// nothing executes) -> execute via operations.h -> upload events -> submit the result -> refresh the
// AI run -> periodic telemetry event. Results are retried with backoff and never re-executed; no new
// job is polled while a result or a running report is pending.
//
// Result delivery invariant (oyster_core/delivery_policy.h): a result cites only event ids that exist in the
// JOB's session AND that the backend accepted; anything else is left out and named in `error`
// ("session_not_active", "evidence_dropped"). A result the backend keeps refusing is retried a bounded number
// of times and then abandoned with a local event, so one undeliverable result never blocks job polling.
#include "assessment_runner.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#include "app_events.h"
#include "backend_client.h"
#include "backend_internal.h"
#include "ble_explorer.h"
#include "device_config.h"
#include "evidence_store.h"
#include "modules.h"
#include "net_link.h"
#include "operations.h"
#include "oyster_core/clock_policy.h"
#include "oyster_core/delivery_policy.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "radio_scheduler.h"
#include "telemetry.h"
#include "time_sync.h"

namespace oyster {

AssessmentRunner g_runner;

namespace {

// The TLS handshake and the operation executors run on this task. op_ble_discover() keeps a
// BleDiscoveryResult (~9.5 KB) and a 2 KB event scratch on the stack, so ~8 KiB is not enough.
// The high-water mark is reported in every telemetry event; tune from measurements.
constexpr uint32_t kStackBytes = 16 * 1024;  // TLS handshake also runs here; shrink only after measuring the hardware high-water mark
constexpr UBaseType_t kQueueDepth = 4;
constexpr UBaseType_t kTaskPriority = 1;     // same as loopTask: round-robin with the UI
constexpr BaseType_t kTaskCore = 1;
constexpr uint32_t kLoopWaitMs = 200;
constexpr uint32_t kRunPollMs = 2000;
constexpr uint32_t kFastJobPollMs = 1000;    // while the AI run waits for this device
constexpr uint32_t kResultDeferMs = 1000;    // evidence still being uploaded: look again soon (not a failed attempt)
constexpr size_t kMaxPendingResults = 2;
constexpr size_t kResultErrorLen = 241;      // schema maxLength 240 + NUL
constexpr uint32_t kStartRetryMinMs = 2000;
constexpr uint32_t kStartRetryMaxMs = 15000;
constexpr const char* kUnsupportedReason = "operation not implemented in this firmware build (milestone M4)";

struct PendingResult {
  bool used;
  char job_id[cfg::kIdBufLen];
  char session_id[cfg::kIdBufLen];  // the JOB's session: cited event ids are checked against it
  Operation op;                     // for the local event when the result is abandoned
  JobOutcome outcome;
  char event_ids[cfg::kMaxResultEventIds][cfg::kIdBufLen];  // candidates until planned, then exactly what is sent
  uint8_t n_events;
  char error[kResultErrorLen];
  bool planned;                     // citations decided: retries resend the identical body (idempotency key)
  bool has_conf;
  LocalConfirmation conf;
  uint32_t next_try_ms;
  uint32_t queued_ms;
  ResultRetryState retry;
};

enum class ConfirmOutcome : uint8_t { Approved, Rejected, Expired };

// An accepted (and, when required, confirmed) job waiting for the backend to accept "running".
struct PendingStart {
  bool active;
  JobSpec job;
  EnrolledTarget target;  // copy: reload_targets() may rewrite the live table meanwhile
  bool has_target;
  bool has_conf;
  LocalConfirmation conf;
  uint32_t next_try_ms;
  uint32_t backoff_ms;
};

QueueHandle_t s_queue = nullptr;
TaskHandle_t s_task = nullptr;
volatile uint32_t s_queue_dropped = 0;
SemaphoreHandle_t s_mu = nullptr;  // session, pending confirmation, decision

std::atomic<bool> s_busy{false};
std::atomic<uint8_t> s_cur_op{static_cast<uint8_t>(Operation::Unknown)};
std::atomic<uint32_t> s_busy_since{0};
std::atomic<bool> s_sync_now{true};
std::atomic<bool> s_reload_targets{true};

char s_session[cfg::kIdBufLen] = "";
PendingConfirmation s_pending{};
uint64_t s_pending_deadline_ms = 0;
LocalConfirmation s_decision{};
bool s_decision_set = false;

// Worker-task state
DeviceConfig s_cfg{};
EnrolledTarget s_targets[cfg::kMaxEnrolledTargets];
size_t s_target_count = 0;
ExecutedJobLedger s_ledger;
PendingResult s_results[kMaxPendingResults];
PendingStart s_start{};
char s_active_run[cfg::kIdBufLen] = "";
bool s_run_active = false;
uint32_t s_last_run_poll_ms = 0;
char s_run_sig_status[24] = "";
char s_run_sig_excerpt[160] = "";
bool s_run_sig_pending = false;
uint32_t s_last_job_poll_ms = 0;
uint32_t s_last_telemetry_ms = 0;
bool s_seed_saved = false;
char s_scratch[cfg::kMaxEventBytes];
JobSpec s_job;
WorkRequest s_req;

class MuLock {
 public:
  MuLock() { xSemaphoreTake(s_mu, portMAX_DELAY); }
  ~MuLock() { xSemaphoreGive(s_mu); }
  MuLock(const MuLock&) = delete;
  MuLock& operator=(const MuLock&) = delete;
};

// Holds RadioPhase::UplinkSync for one network section (never nested: acquire() returns true for
// the phase already held and the inner release would end it early).
class Uplink {
 public:
  Uplink() : held_(g_radio.acquire(RadioPhase::UplinkSync, 0)) {}
  ~Uplink() {
    if (held_) g_radio.release(RadioPhase::UplinkSync);
  }
  bool held() const { return held_; }
  Uplink(const Uplink&) = delete;
  Uplink& operator=(const Uplink&) = delete;

 private:
  bool held_;
};

void toast(const char* text) { app_post(AppEventType::Toast, 0, 0, text); }

void snapshot_session(char out[cfg::kIdBufLen]) {
  MuLock l;
  copy_bounded(out, cfg::kIdBufLen, s_session);
}

void load_targets() {
  g_config.load(s_cfg);
  s_target_count = g_config.load_targets(s_targets, cfg::kMaxEnrolledTargets);
}

// clear_cancel=false for backend jobs: their flag was cleared when the job was accepted, so a cancel pressed
// while the backend was answering "running" is a deliberate cancel and must reach the operation.
void begin_busy(Operation op, bool clear_cancel = true) {
  if (clear_cancel) g_radio.clear_cancel();  // a cancel pressed while idle must not abort the next operation
  s_cur_op.store(static_cast<uint8_t>(op));
  s_busy_since.store(millis());
  s_busy.store(true);
}

void end_busy() {
  s_busy.store(false);
  s_cur_op.store(static_cast<uint8_t>(Operation::Unknown));
}

bool implemented(Operation op) {
  return op == Operation::WifiSurvey || op == Operation::BleDiscover || op == Operation::BleInspectGatt ||
         op == Operation::BleReadProfileValue;
}

bool terminal_run_status(const char* s) {
  static const char* const kTerminal[] = {"succeeded", "failed", "cancelled", "expired", "budget_exhausted",
                                          "ai_unavailable"};
  for (const char* t : kTerminal) {
    if (std::strcmp(s, t) == 0) return true;
  }
  return false;
}

bool run_wants_device() {
  if (!s_run_active) return false;
  const RunSummary& r = g_backend.latest_run();
  return std::strcmp(r.status, "awaiting_device") == 0 || std::strcmp(r.status, "awaiting_local_confirmation") == 0;
}

/// ---- results ----------------------------------------------------------------------------------------

// What happened to the events of ONE session, answered by the evidence store (accepted / skipped / lost /
// session no longer open). A result is checked against its job's session, never against "the current one".
class StoreFates : public EventFateSource {
 public:
  explicit StoreFates(const char* session_id) { copy_bounded(sid_, sizeof(sid_), session_id != nullptr ? session_id : ""); }
  EventFate fate(uint32_t sequence) const override { return g_evidence.event_fate(sid_, sequence); }

 private:
  char sid_[cfg::kIdBufLen];
};

PendingResult* find_result(const char* job_id) {
  for (auto& r : s_results) {
    if (r.used && std::strcmp(r.job_id, job_id) == 0) return &r;
  }
  return nullptr;
}

size_t pending_result_count() {
  size_t n = 0;
  for (const auto& r : s_results) n += r.used ? 1 : 0;
  return n;
}

bool results_due(uint32_t now) {
  for (const auto& r : s_results) {
    if (r.used && static_cast<int32_t>(now - r.next_try_ms) >= 0) return true;
  }
  return false;
}

// `ids` are only CANDIDATES to cite: retry_results() keeps the ones the job's session holds and the backend
// accepted, and says so in `error` for the rest.
void queue_result(const JobSpec& job, JobOutcome outcome, const char* const* ids, size_t n, const char* error,
                  const LocalConfirmation* conf) {
  PendingResult* slot = find_result(job.job_id);
  if (slot == nullptr) {
    for (auto& r : s_results) {
      if (!r.used) {
        slot = &r;
        break;
      }
    }
  }
  if (slot == nullptr) {
    // Cannot happen while new jobs wait for pending results; keep the newest, report the loss.
    slot = &s_results[0];
    for (auto& r : s_results) {
      if (r.queued_ms < slot->queued_ms) slot = &r;
    }
    log_e("runner: result queue full; dropping the result of %s", slot->job_id);
  }
  std::memset(static_cast<void*>(slot), 0, sizeof(*slot));
  slot->used = true;
  copy_bounded(slot->job_id, sizeof(slot->job_id), job.job_id);
  copy_bounded(slot->session_id, sizeof(slot->session_id), job.session_id);
  slot->op = job.op;
  slot->outcome = outcome;
  for (size_t i = 0; i < n && i < cfg::kMaxResultEventIds; ++i) {
    if (ids != nullptr && ids[i] != nullptr && ids[i][0] != '\0') {
      copy_bounded(slot->event_ids[slot->n_events++], cfg::kIdBufLen, ids[i]);
    }
  }
  if (error != nullptr) copy_bounded(slot->error, sizeof(slot->error), error);
  if (conf != nullptr) {
    slot->has_conf = true;
    slot->conf = *conf;
  }
  slot->queued_ms = millis();
  slot->next_try_ms = millis();
}

void queue_op_result(const JobSpec& job, const OpResult& r, const LocalConfirmation* conf) {
  const char* ids[cfg::kMaxResultEventIds];
  const size_t n = r.event_count < cfg::kMaxResultEventIds ? r.event_count : cfg::kMaxResultEventIds;
  for (size_t i = 0; i < n; ++i) ids[i] = r.event_ids[i];
  queue_result(job, r.outcome, ids, n, r.error[0] != '\0' ? r.error : nullptr, conf);
}

// The device gave up delivering a result: say so in the evidence log (the job itself ends on the backend by
// its deadline). The event goes to the current session; it names the job's session.
void write_abandon_event(const PendingResult& r, const char* reason, int http_status, const char* error_code) {
  EventHeader h{r.op, Transport::Backend, EventStatus::Error, SourceMode::Live, nullptr};
  JsonWriter& w = g_evidence.begin_event(h, s_scratch, sizeof(s_scratch));
  w.str("job_id", r.job_id);
  w.str("job_session_id", r.session_id[0] != '\0' ? r.session_id : nullptr);
  w.str("verdict", "result_abandoned");
  w.str("reason", reason);
  w.str("result_outcome", to_string(r.outcome));
  w.unum("cited_event_count", r.n_events);
  w.unum("refused_attempts", r.retry.refused_attempts);
  w.unum("age_ms", millis() - r.queued_ms);
  w.num("last_http_status", http_status);
  w.str("last_error_code", error_code != nullptr && error_code[0] != '\0' ? error_code : nullptr);
  char id[cfg::kIdBufLen];
  g_evidence.commit_event(id);
}

// Decides what a result may cite (once; retries resend the identical body so the idempotency key stays valid).
// Returns false while cited events are still being uploaded (look again later, not a failed attempt).
bool plan_result_citations(PendingResult& r, uint32_t age_ms) {
  if (r.planned) return true;
  const char* ids[cfg::kMaxResultEventIds];
  for (size_t i = 0; i < r.n_events; ++i) ids[i] = r.event_ids[i];
  const StoreFates fates(r.session_id);
  const CitationPlan plan = plan_citations(ids, r.n_events, fates);
  const bool may_wait = result_may_wait(age_ms);
  if (plan.pending > 0 && may_wait) return false;
  char note[kResultErrorLen];
  describe_uncited(plan, !may_wait, note, sizeof(note));
  if (note[0] != '\0') {
    char merged[kResultErrorLen];
    compose_result_error(r.error, note, merged, sizeof(merged));
    copy_bounded(r.error, sizeof(r.error), merged);
    log_w("runner: result of %s cites %u of %u events: %s", r.job_id, static_cast<unsigned>(plan.citable),
          static_cast<unsigned>(plan.total), note);
  }
  for (size_t i = 0; i < plan.citable; ++i) copy_bounded(r.event_ids[i], cfg::kIdBufLen, plan.ids[i]);
  r.n_events = plan.citable;
  r.planned = true;
  return true;
}

// Caller holds the uplink and has just uploaded evidence, so results never reference missing events.
void retry_results(bool force) {
  for (auto& r : s_results) {
    if (!r.used) continue;
    const uint32_t now = millis();
    if (!force && static_cast<int32_t>(now - r.next_try_ms) < 0) continue;
    if (!plan_result_citations(r, now - r.queued_ms)) {
      r.next_try_ms = now + kResultDeferMs;  // cited evidence is still being uploaded
      continue;
    }
    const char* ids[cfg::kMaxResultEventIds];
    for (size_t i = 0; i < r.n_events; ++i) ids[i] = r.event_ids[i];
    if (g_backend.submit_result(r.job_id, r.outcome, ids, r.n_events, r.error[0] != '\0' ? r.error : nullptr,
                                r.has_conf ? &r.conf : nullptr)) {
      r.used = false;
      continue;
    }
    const int http_status = backend_detail::last_http_status();
    const char* code = backend_detail::last_error_code();
    const SubmitFailure kind =
        backend_detail::last_submit_permanent() ? SubmitFailure::Refused : classify_submit_failure(http_status, code);
    const RetryDecision d = decide_result_retry(r.retry, kind, millis() - r.queued_ms);
    if (d.abandon) {
      // Never block remote jobs forever: stop trying, leave a local event, let job polling continue.
      log_e("runner: result of %s abandoned (%s; HTTP %d %s)", r.job_id, d.reason, http_status, code);
      write_abandon_event(r, d.reason, http_status, code);
      toast("Job result abandoned: not delivered");
      r.used = false;
      s_sync_now.store(true);
      continue;
    }
    r.next_try_ms = millis() + d.delay_ms;
  }
}

void sync_results_now() {
  Uplink up;
  if (!up.held()) return;
  g_backend.upload_pending_events();
  retry_results(true);
}

void report_status(const char* job_id, const char* status) {
  Uplink up;
  if (up.held()) g_backend.report_job_status(job_id, status);
}

// ---- evidence written by the runner ---------------------------------------------------------------

// A remote job the device refused or could not start: evidence that the device enforced its policy.
void write_job_event(const JobSpec& job, EventStatus st, const char* verdict, const char* reason,
                     char out_id[cfg::kIdBufLen]) {
  out_id[0] = '\0';
  const char* target = is_valid_target_id(job.target_id) ? job.target_id : nullptr;
  EventHeader h{job.op, Transport::Local, st, SourceMode::Live, target};
  JsonWriter& w = g_evidence.begin_event(h, s_scratch, sizeof(s_scratch));
  w.str("job_id", job.job_id);
  w.str("operation_requested", job.op_wire);
  w.str("verdict", verdict);
  w.str("reason", reason);
  w.boolean("executed", false);
  // The event lands in the session open NOW. When that is not the job's session (stale job, session switch)
  // it is still evidence of the refusal, but it must not be cited by the job's result.
  if (!session_citable(job.session_id, g_evidence.session_id())) {
    w.str("job_session_id", job.session_id[0] != '\0' ? job.session_id : nullptr);
    w.boolean("in_job_session", false);
  }
  g_evidence.commit_event(out_id);
}

void write_confirmation_event(const JobSpec& job, ConfirmOutcome outcome, bool approved_by_user, uint64_t waited_ms,
                              char out_id[cfg::kIdBufLen]) {
  out_id[0] = '\0';
  const char* target = is_valid_target_id(job.target_id) ? job.target_id : nullptr;
  const EventStatus st = outcome == ConfirmOutcome::Approved  ? EventStatus::Success
                         : outcome == ConfirmOutcome::Expired ? EventStatus::Timeout
                                                              : EventStatus::Denied;
  EventHeader h{Operation::LocalConfirmation, Transport::Local, st, SourceMode::Live, target};
  JsonWriter& w = g_evidence.begin_event(h, s_scratch, sizeof(s_scratch));
  w.str("job_id", job.job_id);
  w.str("operation", job.op_wire);
  w.str("decision", outcome == ConfirmOutcome::Approved  ? "approved"
                    : outcome == ConfirmOutcome::Expired ? "expired"
                                                         : "rejected");
  w.boolean("approved_by_user", approved_by_user);
  w.boolean("valid", outcome == ConfirmOutcome::Approved);
  w.unum("waited_ms", waited_ms);
  w.unum("timeout_ms", cfg::kLocalConfirmTimeoutMs);
  w.str("rule", "operation_type");
  g_evidence.commit_event(out_id);
}

void telemetry_tick(bool force) {
  const uint32_t now = millis();
  if (!force && now - s_last_telemetry_ms < cfg::kTelemetryPeriodMs) return;
  s_last_telemetry_ms = now;
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  if (sid[0] == '\0' || g_evidence.session_id()[0] == '\0') return;
  TelemetrySnapshot snap{};
  telemetry_snapshot(snap);
  EventHeader h{Operation::Telemetry, Transport::None, EventStatus::Success, SourceMode::Live, nullptr};
  JsonWriter& w = g_evidence.begin_event(h, s_scratch, sizeof(s_scratch));
  telemetry_write_json(snap, w);
  w.begin_object("evidence_store");
  w.boolean("sd_backed", g_evidence.sd_backed());
  w.unum("dropped_events", g_evidence.dropped_events());
  w.unum("acked_sequence", g_evidence.acked_sequence());
  w.unum("resolved_sequence", g_evidence.resolved_sequence());
  w.unum("skipped_events", g_evidence.skipped_events());
  w.end_object();
  w.begin_object("backend_link");
  w.str("state", to_string(g_backend.state()));
  const bool wifi = net::up();
  w.boolean("wifi_up", wifi);
  if (wifi) {
    w.num("wifi_rssi_dbm", net::rssi());
  } else {
    w.null("wifi_rssi_dbm");
  }
  w.str("clock_quality", to_string(timesync::quality()));
  w.unum("rejected_events", backend_detail::rejected_events());
  w.end_object();
  w.begin_object("runner");
  w.unum("queue_dropped", s_queue_dropped);
  w.unum("pending_results", pending_result_count());
  w.unum("jobs_started", s_ledger.size());
  w.end_object();
  char id[cfg::kIdBufLen];
  g_evidence.commit_event(id);
}

// ---- AI run -----------------------------------------------------------------------------------------

void note_run(const RunSummary& rs) {
  const bool changed = std::strcmp(rs.status, s_run_sig_status) != 0 ||
                       std::strcmp(rs.answer_excerpt, s_run_sig_excerpt) != 0 ||
                       rs.has_pending_action != s_run_sig_pending;
  copy_bounded(s_run_sig_status, sizeof(s_run_sig_status), rs.status);
  copy_bounded(s_run_sig_excerpt, sizeof(s_run_sig_excerpt), rs.answer_excerpt);
  s_run_sig_pending = rs.has_pending_action;
  if (terminal_run_status(rs.status)) s_run_active = false;
  if (changed) app_post(AppEventType::RunUpdated);
}

void refresh_run(bool force) {
  if (!s_run_active || s_active_run[0] == '\0') return;
  if (!force && millis() - s_last_run_poll_ms < kRunPollMs) return;
  if (!g_radio.uplink_allowed()) return;
  s_last_run_poll_ms = millis();
  RunSummary rs{};
  bool ok = false;
  {
    Uplink up;
    if (!up.held()) return;
    ok = g_backend.poll_run(s_active_run, rs);
  }
  if (ok) note_run(rs);
}

void ask(const char* question) {
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  if (sid[0] == '\0') {
    toast("Open a session first");
    return;
  }
  if (!s_cfg.provisioned) {
    toast("Not provisioned: AI backend unavailable");
    return;
  }
  char run_id[cfg::kIdBufLen] = "";
  bool started = false;
  {
    Uplink up;
    if (!up.held()) {
      toast("Radio busy; try again");
      return;
    }
    g_backend.upload_pending_events();  // the run sees the latest evidence
    started = g_backend.start_run(sid, question, run_id);
  }
  if (!started) {
    char msg[64];
    snprintf(msg, sizeof(msg), "AI request failed: %s", backend_detail::last_error());
    toast(msg);
    return;
  }
  copy_bounded(s_active_run, sizeof(s_active_run), run_id);
  s_run_active = true;
  s_run_sig_status[0] = '\0';
  s_run_sig_excerpt[0] = '\0';
  s_run_sig_pending = false;
  app_post(AppEventType::RunUpdated);
  refresh_run(true);
}

void cancel_run() {
  if (s_active_run[0] == '\0') {
    toast("No active AI run");
    return;
  }
  bool ok = false;
  {
    Uplink up;
    if (up.held()) ok = g_backend.cancel_run(s_active_run);
  }
  if (!ok) toast("AI run cancel request failed");
  s_run_active = true;  // poll once more to show the final status
  refresh_run(true);
}

// ---- local confirmation -------------------------------------------------------------------------------

void effect_text(const JobSpec& job, char* out, size_t n) {
  const char* t = job.target_id[0] != '\0' ? job.target_id : "the target";
  switch (job.op) {
    case Operation::BleWriteLabValue:
      snprintf(out, n, "Writes a lab value on %s (target state changes)", t);
      break;
    case Operation::BleCollectDiagnostics:
      snprintf(out, n, "Copies the synthetic diagnostic object of %s to SD", t);
      break;
    default:
      snprintf(out, n, "Runs %s on %s", job.op_wire, t);
      break;
  }
}

ConfirmOutcome await_confirmation(const JobSpec& job, LocalConfirmation& conf, char event_id[cfg::kIdBufLen]) {
  report_status(job.job_id, "awaiting_local_confirmation");
  {
    MuLock l;
    std::memset(&s_pending, 0, sizeof(s_pending));
    s_pending.active = true;
    copy_bounded(s_pending.job_id, sizeof(s_pending.job_id), job.job_id);
    copy_bounded(s_pending.nonce, sizeof(s_pending.nonce), job.nonce);
    copy_bounded(s_pending.operation, sizeof(s_pending.operation), job.op_wire);
    copy_bounded(s_pending.target_id, sizeof(s_pending.target_id), job.target_id);
    effect_text(job, s_pending.effect, sizeof(s_pending.effect));
    s_pending_deadline_ms = job.received_ms + cfg::kLocalConfirmTimeoutMs;
    s_decision_set = false;
    std::memset(&s_decision, 0, sizeof(s_decision));
  }
  g_radio.clear_cancel();
  app_post(AppEventType::ConfirmationRequested, static_cast<uint32_t>(job.op));
  const uint64_t start = timesync::uptime_ms();
  ConfirmOutcome outcome = ConfirmOutcome::Expired;
  bool decided = false;
  LocalConfirmation dec{};
  for (;;) {
    {
      MuLock l;
      if (s_decision_set) {
        dec = s_decision;
        decided = true;
      }
    }
    if (decided) {
      if (!dec.approved) {
        outcome = ConfirmOutcome::Rejected;
      } else {
        // Exact job id + nonce, decided within the timeout; otherwise the approval is not valid.
        outcome = confirmation_valid(job, dec, cfg::kLocalConfirmTimeoutMs) ? ConfirmOutcome::Approved
                                                                          : ConfirmOutcome::Expired;
      }
      break;
    }
    if (g_radio.cancel_requested()) {
      g_radio.clear_cancel();
      outcome = ConfirmOutcome::Rejected;
      break;
    }
    if (timesync::uptime_ms() > s_pending_deadline_ms) {
      outcome = ConfirmOutcome::Expired;
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    refresh_run(false);
    telemetry_tick(false);
  }
  {
    MuLock l;
    s_pending.active = false;
  }
  std::memset(&conf, 0, sizeof(conf));
  copy_bounded(conf.job_id, sizeof(conf.job_id), job.job_id);
  copy_bounded(conf.nonce, sizeof(conf.nonce), job.nonce);
  conf.approved = outcome == ConfirmOutcome::Approved;
  conf.decided_ms = decided ? dec.decided_ms : timesync::uptime_ms();
  const uint32_t closed = outcome == ConfirmOutcome::Approved ? 1u : (outcome == ConfirmOutcome::Expired ? 2u : 0u);
  app_post(AppEventType::ConfirmationClosed, closed);
  write_confirmation_event(job, outcome, decided && dec.approved, timesync::uptime_ms() - start, event_id);
  return outcome;
}

// ---- execution ----------------------------------------------------------------------------------------

OpResult arg_error_result(const char* err) {
  OpResult r{};
  r.status = EventStatus::Error;
  r.outcome = JobOutcome::Failed;
  r.event_count = 0;
  snprintf(r.error, sizeof(r.error), "invalid arguments: %s", err[0] != '\0' ? err : "rejected");
  return r;
}

OpResult execute_job(const JobSpec& job, const EnrolledTarget* target, const char* sid) {
  OpContext ctx{sid, &job, target, ""};
  char err[96] = "";
  OpResult r{};
  begin_busy(job.op, false);  // keep a cancel pressed since the job was accepted
  switch (job.op) {
    case Operation::WifiSurvey: {
      WifiSurveyConfig c;
      r = parse_wifi_survey_args(job, c, err, sizeof(err)) ? op_wifi_survey(ctx, c) : arg_error_result(err);
      break;
    }
    case Operation::BleDiscover: {
      BleDiscoveryConfig c;
      r = parse_ble_discover_args(job, c, err, sizeof(err)) ? op_ble_discover(ctx, c) : arg_error_result(err);
      break;
    }
    case Operation::BleInspectGatt:
      r = op_ble_inspect_gatt(ctx);
      break;
    case Operation::BleReadProfileValue: {
      char cid[24] = "";
      r = parse_ble_read_args(job, cid, sizeof(cid), err, sizeof(err)) ? op_ble_read_profile_value(ctx, cid)
                                                                       : arg_error_result(err);
      break;
    }
    default:
      r = op_unsupported(ctx, job.op, kUnsupportedReason);
      break;
  }
  end_busy();
  if (r.event_count == 0 && r.outcome == JobOutcome::Failed && r.error[0] != '\0') {
    char id[cfg::kIdBufLen];
    write_job_event(job, EventStatus::Error, "invalid_arguments", r.error, id);
    if (id[0] != '\0') {
      copy_bounded(r.event_ids[0], cfg::kIdBufLen, id);
      r.event_count = 1;
    }
  }
  return r;
}

// Pre-execution gate: POST /v1/jobs/{id}/status "running" must return 200 before anything runs.
// 4xx refusal (409: expired/cancelled by the backend) -> not executed, local event, no result.
// Network failure -> not executed yet; the report is retried with backoff, never assumed.
void finish_start_without_running(EventStatus st, const char* verdict, const char* reason, bool submit,
                                  JobOutcome outcome) {
  JobSpec& job = s_start.job;
  char id[cfg::kIdBufLen];
  write_job_event(job, st, verdict, reason, id);
  if (submit) {
    const char* ids[1] = {id};
    queue_result(job, outcome, ids, id[0] != '\0' ? 1 : 0, reason, s_start.has_conf ? &s_start.conf : nullptr);
  }
  log_w("runner: job %s not executed: %s", job.job_id, reason);
  s_start.active = false;
  s_sync_now.store(true);
}

// The runner's session, the evidence store's open session and the job's session must all be the same, or the
// job's events would land in (or be cited from) another session.
bool job_session_current(const JobSpec& job) {
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  return std::strcmp(sid, job.session_id) == 0 && session_citable(job.session_id, g_evidence.session_id());
}

// An approval is only used while it is fresh at the moment of use, not just when the user answered.
bool start_confirmation_stale(const JobSpec& job) {
  return s_start.has_conf &&
         !confirmation_fresh_for_execution(job, s_start.conf, timesync::uptime_ms(), cfg::kLocalConfirmTimeoutMs);
}

void try_start_job() {
  if (!s_start.active) return;
  if (static_cast<int32_t>(millis() - s_start.next_try_ms) < 0) return;
  JobSpec& job = s_start.job;
  if (g_radio.cancel_requested()) {
    g_radio.clear_cancel();
    finish_start_without_running(EventStatus::Cancelled, "cancelled_on_device",
                                 "cancelled on the device before the backend accepted running; not executed", true,
                                 JobOutcome::Cancelled);
    toast("Remote job cancelled");
    return;
  }
  if (timesync::uptime_ms() > job.received_ms + job.deadline_ms) {
    finish_start_without_running(EventStatus::Cancelled, "deadline_passed",
                                 "deadline passed before the backend accepted running; not executed", true,
                                 JobOutcome::Expired);
    return;
  }
  if (!job_session_current(job)) {
    finish_start_without_running(EventStatus::Cancelled, "session_changed",
                                 "the active session changed before the job could start; not executed", true,
                                 JobOutcome::Failed);
    return;
  }
  if (start_confirmation_stale(job)) {
    finish_start_without_running(EventStatus::Cancelled, "confirmation_expired",
                                 "the local confirmation was no longer fresh before the job could start; not executed",
                                 true, JobOutcome::Expired);
    return;
  }
  if (!g_radio.uplink_allowed()) return;
  bool ok = false;
  int st = 0;
  {
    Uplink up;
    if (!up.held()) return;
    ok = g_backend.report_job_status(job.job_id, "running");
    st = backend_detail::last_http_status();
  }
  if (ok) {
    // The flag was cleared when the job was accepted, so a cancel pressed during the "running" request above is
    // still set here: honour it (the backend already has "running", so the cancel is reported as the result).
    if (g_radio.cancel_requested()) {
      g_radio.clear_cancel();
      finish_start_without_running(EventStatus::Cancelled, "cancelled_on_device",
                                   "cancelled on the device while the backend was accepting running; not executed", true,
                                   JobOutcome::Cancelled);
      toast("Remote job cancelled");
      sync_results_now();
      return;
    }
    // The HTTPS request above can take seconds: re-check session and confirmation freshness right before running.
    if (!job_session_current(job)) {
      finish_start_without_running(EventStatus::Cancelled, "session_changed",
                                   "the active session changed while the backend was accepting running; not executed",
                                   true, JobOutcome::Failed);
      sync_results_now();
      return;
    }
    if (start_confirmation_stale(job)) {
      finish_start_without_running(EventStatus::Cancelled, "confirmation_expired",
                                   "the local confirmation was no longer fresh when the job was about to run; not executed",
                                   true, JobOutcome::Expired);
      sync_results_now();
      return;
    }
    s_start.active = false;
    char sid[cfg::kIdBufLen];
    snapshot_session(sid);
    const OpResult r = execute_job(job, s_start.has_target ? &s_start.target : nullptr, sid);
    queue_op_result(job, r, s_start.has_conf ? &s_start.conf : nullptr);
    sync_results_now();
    return;
  }
  if (st >= 400 && st < 500 && st != 401 && st != 403 && st != 408 && st != 429) {
    char reason[96];
    snprintf(reason, sizeof(reason), "backend refused running (HTTP %d %s); not executed", st,
             backend_detail::last_error_code());
    finish_start_without_running(EventStatus::Cancelled, "running_refused", reason, false,
                                 JobOutcome::Cancelled);  // no result is submitted
    toast("Remote job withdrawn by backend; not executed");
    return;
  }
  s_start.backoff_ms = s_start.backoff_ms == 0
                           ? kStartRetryMinMs
                           : (s_start.backoff_ms * 2 > kStartRetryMaxMs ? kStartRetryMaxMs : s_start.backoff_ms * 2);
  s_start.next_try_ms = millis() + s_start.backoff_ms;
}

JobOutcome outcome_for(PolicyVerdict v) {
  switch (v) {
    case PolicyVerdict::RejectUnknownOperation: return JobOutcome::Unsupported;
    case PolicyVerdict::RejectExpired: return JobOutcome::Expired;
    default: return JobOutcome::Failed;
  }
}

void handle_job(const JobSpec& job) {
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  if (s_ledger.contains(job.job_id)) {
    // Re-delivered after it was accepted here: report, never re-execute.
    if (s_start.active && std::strcmp(s_start.job.job_id, job.job_id) == 0) return;
    if (find_result(job.job_id) == nullptr) {
      queue_result(job, JobOutcome::UnknownOutcome, nullptr, 0,
                   "job id was already started on this device; not re-executed", nullptr);
    }
    sync_results_now();
    return;
  }
  const char* parse_err = backend_detail::job_parse_error();
  PolicyInput in{s_cfg.device_id, sid, timesync::uptime_ms(), g_backend.state() == BackendState::Connected, s_targets,
                 s_target_count};
  const PolicyDecision d = evaluate_job(job, in, s_ledger);
  if (d.verdict != PolicyVerdict::Accept) {
    char err[96];
    if (parse_err[0] != '\0') {
      snprintf(err, sizeof(err), "%s (local policy %s)", parse_err, to_string(d.verdict));
    } else {
      snprintf(err, sizeof(err), "local policy %s: %s", to_string(d.verdict), d.reason != nullptr ? d.reason : "");
    }
    char id[cfg::kIdBufLen];
    const EventStatus st = d.verdict == PolicyVerdict::RejectUnknownOperation ? EventStatus::Unsupported : EventStatus::Denied;
    write_job_event(job, st, to_string(d.verdict), err, id);
    // The denial event is written into the session open now. Only when that is the JOB's session may the result
    // cite it; a job of another session (stale job after a reboot or "New session") gets a failed result that
    // cites nothing and says why - otherwise the backend answers 409 events_missing for ever.
    const bool in_job_session = session_citable(job.session_id, g_evidence.session_id());
    const char* ids[1] = {id};
    char result_err[kResultErrorLen];
    compose_result_error(err,
                         in_job_session ? "" : "session_not_active: the job belongs to another session than the open "
                                               "one; no local evidence cited",
                         result_err, sizeof(result_err));
    queue_result(job, outcome_for(d.verdict), ids, in_job_session && id[0] != '\0' ? 1 : 0, result_err, nullptr);
    log_w("runner: job %s rejected: %s", job.job_id, err);
    toast("Remote job rejected by local policy");
    sync_results_now();
    return;
  }
  s_ledger.record(job.job_id);  // before anything runs: a job id is never executed twice
  if (job.server_requires_confirmation != d.requires_local_confirmation) {
    log_w("runner: job %s confirmation flag differs from the operation rule; the device rule applies", job.job_id);
  }

  // Operations not implemented in this build run nothing, so no confirmation is requested for them;
  // they still pass the running gate below and report Unsupported with the reason.
  LocalConfirmation conf{};
  bool have_conf = false;
  if (implemented(job.op) && d.requires_local_confirmation) {
    char conf_event[cfg::kIdBufLen];
    const ConfirmOutcome co = await_confirmation(job, conf, conf_event);
    have_conf = true;
    if (co != ConfirmOutcome::Approved) {
      const char* ids[1] = {conf_event};
      queue_result(job, co == ConfirmOutcome::Expired ? JobOutcome::Expired : JobOutcome::Cancelled, ids,
                   conf_event[0] != '\0' ? 1 : 0,
                   co == ConfirmOutcome::Expired ? "local confirmation timed out or was not valid"
                                                 : "local confirmation rejected on the device",
                   &conf);
      sync_results_now();
      return;
    }
  }
  // Execution waits for the backend to accept "running" (try_start_job).
  std::memset(&s_start, 0, sizeof(s_start));
  s_start.job = job;
  if (d.target != nullptr) {
    s_start.target = *d.target;
    s_start.has_target = true;
  }
  s_start.has_conf = have_conf;
  s_start.conf = conf;
  s_start.next_try_ms = millis();
  s_start.active = true;
  g_radio.clear_cancel();  // from here a cancel press is a deliberate cancel of this job
  try_start_job();
}

// ---- UI requests ------------------------------------------------------------------------------------

const EnrolledTarget* ui_target(const char* target_id, Operation op) {
  for (size_t i = 0; i < s_target_count; ++i) {
    if (std::strcmp(s_targets[i].target_id, target_id) == 0) {
      if ((s_targets[i].allowed_ops_mask & op_bit(op)) == 0) {
        toast("Operation not allowed for this target");
        return nullptr;
      }
      return &s_targets[i];
    }
  }
  toast("Target is not enrolled on this device");
  return nullptr;
}

void handle_request(WorkRequest& r) {
  r.room_label[sizeof(r.room_label) - 1] = '\0';
  r.target_id[sizeof(r.target_id) - 1] = '\0';
  r.characteristic_id[sizeof(r.characteristic_id) - 1] = '\0';
  r.question[sizeof(r.question) - 1] = '\0';
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  const bool needs_session = r.kind == RequestKind::WifiSurvey || r.kind == RequestKind::BleDiscover ||
                             r.kind == RequestKind::InspectGatt || r.kind == RequestKind::ReadValue;
  if (needs_session && sid[0] == '\0') {
    toast("Open a session first");
    return;
  }
  switch (r.kind) {
    case RequestKind::WifiSurvey: {
      WifiSurveyConfig c;
      c.passive = !r.active_scan;
      if (r.duration_ms > 0) {
        // duration_ms is the whole-survey budget over channels 1-13.
        uint32_t per = r.duration_ms / 13;
        const uint32_t max_per = cfg::kWifiScanMaxMs / 13;
        if (per < 100) per = 100;
        if (per > max_per) per = max_per;
        c.ms_per_channel = per;
      }
      OpContext ctx{sid, nullptr, nullptr, r.room_label};
      begin_busy(Operation::WifiSurvey);
      op_wifi_survey(ctx, c);
      end_busy();
      break;
    }
    case RequestKind::BleDiscover: {
      BleDiscoveryConfig c;
      c.active = r.active_scan;
      if (r.duration_ms > 0) c.duration_ms = r.duration_ms < cfg::kBleDiscoveryMaxMs ? r.duration_ms : cfg::kBleDiscoveryMaxMs;
      OpContext ctx{sid, nullptr, nullptr, ""};
      begin_busy(Operation::BleDiscover);
      op_ble_discover(ctx, c);
      end_busy();
      break;
    }
    case RequestKind::InspectGatt: {
      const EnrolledTarget* t = ui_target(r.target_id, Operation::BleInspectGatt);
      if (t == nullptr) return;
      OpContext ctx{sid, nullptr, t, ""};
      begin_busy(Operation::BleInspectGatt);
      op_ble_inspect_gatt(ctx);
      end_busy();
      break;
    }
    case RequestKind::ReadValue: {
      const EnrolledTarget* t = ui_target(r.target_id, Operation::BleReadProfileValue);
      if (t == nullptr) return;
      OpContext ctx{sid, nullptr, t, ""};
      begin_busy(Operation::BleReadProfileValue);
      op_ble_read_profile_value(ctx, r.characteristic_id);
      end_busy();
      break;
    }
    case RequestKind::AskOyster:
      ask(r.question);
      break;
    case RequestKind::CancelRun:
      cancel_run();
      break;
    case RequestKind::SyncNow:
      break;
    case RequestKind::Module: {
      Uplink up;
      if (!up.held()) {
        toast("Radio busy; try again");
        modules::changed();
        break;
      }
      modules::run_call(r.module_id, r.call_id, r.arg);
      break;
    }
  }
  s_sync_now.store(true);  // upload the new evidence soon
}

// ---- background work ----------------------------------------------------------------------------------

void network_cycle() {
  if (!s_cfg.provisioned || !g_radio.uplink_allowed()) return;
  const uint32_t now = millis();
  const uint32_t interval = run_wants_device() ? kFastJobPollMs : cfg::kBackendPollMs;
  if (!s_sync_now.load() && !results_due(now) && now - s_last_job_poll_ms < interval) return;
  s_sync_now.store(false);
  s_last_job_poll_ms = now;
  char sid[cfg::kIdBufLen];
  snapshot_session(sid);
  bool have_job = false;
  {
    Uplink up;
    if (!up.held()) return;
    if (sid[0] != '\0' && g_evidence.session_id()[0] != '\0') g_backend.upload_pending_events();
    retry_results(false);
    // Jobs need an active session; no new remote action while a result is still undelivered.
    if (sid[0] != '\0' && pending_result_count() == 0 && !s_start.active) have_job = g_backend.poll_job(s_job);
  }
  if (have_job) handle_job(s_job);
}

void save_clock_seed() {
  if (s_seed_saved || !timesync::ntp_synced_this_boot()) return;
  s_seed_saved = true;
  uint64_t now = 0;
  DeviceConfig c{};
  if (!timesync::now_unix(now) || !g_config.load(c)) return;
  // The floor is "last known good UTC": a later clock earlier than this is implausible. NTP is unauthenticated,
  // so it may move the persisted floor forward by at most 400 days; a bigger jump (a spoofed far-future reply)
  // is used for the RAM clock only and is NOT persisted.
  const uint64_t floor = c.time_seed_unix > timesync::kMinPlausibleUnix ? c.time_seed_unix : timesync::kMinPlausibleUnix;
  if (ntp_floor_may_advance(now, floor)) {
    c.time_seed_unix = now;
    g_config.save(c);
  } else if (now > floor) {
    log_w("runner: NTP time is more than %u days past the stored time floor; not persisted (RAM clock only)",
          static_cast<unsigned>(kMaxNtpFloorAdvanceSeconds / 86400ULL));
  }
}

void worker(void*) {
  for (;;) {
    if (xQueueReceive(s_queue, &s_req, pdMS_TO_TICKS(kLoopWaitMs)) == pdTRUE) handle_request(s_req);
    if (s_reload_targets.exchange(false)) load_targets();
    try_start_job();
    network_cycle();
    refresh_run(false);
    telemetry_tick(false);
    save_clock_seed();
  }
}

}  // namespace

bool AssessmentRunner::begin() {
  if (s_task != nullptr) return true;
  if (s_mu == nullptr) s_mu = xSemaphoreCreateMutex();
  if (s_queue == nullptr) s_queue = xQueueCreate(kQueueDepth, sizeof(WorkRequest));
  if (s_mu == nullptr || s_queue == nullptr) return false;
  load_targets();
  s_reload_targets.store(false);
  s_last_telemetry_ms = millis();
  telemetry_register_queue("runner", s_queue, static_cast<uint16_t>(kQueueDepth), &s_queue_dropped);
  const BaseType_t ok =
      xTaskCreatePinnedToCore(worker, "oyster-worker", kStackBytes, nullptr, kTaskPriority, &s_task, kTaskCore);
  if (ok != pdPASS) {
    s_task = nullptr;
    log_e("runner: worker task creation failed");
    return false;
  }
  telemetry_register_task("oyster-worker", s_task);
  return true;
}

bool AssessmentRunner::enqueue(const WorkRequest& r) {
  if (s_queue == nullptr) return false;
  if (xQueueSend(s_queue, &r, 0) != pdTRUE) {
    s_queue_dropped = s_queue_dropped + 1;
    return false;
  }
  return true;
}

bool AssessmentRunner::busy() const { return s_busy.load(); }

Operation AssessmentRunner::current_op() const {
  return s_busy.load() ? static_cast<Operation>(s_cur_op.load()) : Operation::Unknown;
}

uint32_t AssessmentRunner::busy_elapsed_ms() const { return s_busy.load() ? millis() - s_busy_since.load() : 0; }

void AssessmentRunner::cancel_current() {
  g_radio.request_cancel();
  g_ble.cancel();  // safe from any task: interrupts a pending connect / drops the link mid-GATT step
}

bool AssessmentRunner::pending_confirmation(PendingConfirmation& out) const {
  std::memset(&out, 0, sizeof(out));
  if (s_mu == nullptr) return false;
  MuLock l;
  out = s_pending;
  if (out.active) {
    const uint64_t now = timesync::uptime_ms();
    out.remaining_ms = now < s_pending_deadline_ms ? static_cast<uint32_t>(s_pending_deadline_ms - now) : 0;
  }
  return out.active;
}

void AssessmentRunner::resolve_confirmation(const LocalConfirmation& c) {
  if (s_mu == nullptr) return;
  MuLock l;
  if (!s_pending.active || std::strncmp(c.job_id, s_pending.job_id, cfg::kIdBufLen) != 0) return;
  s_decision = c;
  s_decision.decided_ms = timesync::uptime_ms();  // the device clock decides timing, not the caller
  s_decision_set = true;
}

void AssessmentRunner::set_session(const char* session_id) {
  if (s_mu == nullptr) s_mu = xSemaphoreCreateMutex();
  {
    MuLock l;
    copy_bounded(s_session, sizeof(s_session), session_id != nullptr ? session_id : "");
  }
  s_sync_now.store(true);
}

const char* AssessmentRunner::session() const { return s_session; }

void AssessmentRunner::reload_targets() { s_reload_targets.store(true); }

}  // namespace oyster
