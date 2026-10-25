// Operation executors shared by UI actions and backend jobs. Each executor acquires its radio phase,
// runs bounded work while polling g_radio.cancel_requested(), writes evidence (summary event + paged
// observation events), updates g_results, posts AppEvents, and releases the phase. Blocking: worker
// task only.
//
// Truthfulness rules kept here: anything dropped, overflowed or limited makes the summary
// complete:false and the event status Partial; absent values are null; a remote job (ctx.job != nullptr)
// is held to its limits and deadline.
#include "operations.h"

#include <Arduino.h>
#include <ArduinoJson.h>

#include <cstdio>
#include <cstring>

#include "app_events.h"
#include "evidence_store.h"
#include "oyster_core/event_paging.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "oyster_core/lab_identity.h"
#include "oyster_core/lab_profile_gen.h"
#include "oyster_core/obs_json.h"
#include "oyster_core/sha256.h"
#include "radio_scheduler.h"
#include "results_cache.h"
#include "time_sync.h"

namespace oyster {

namespace {

constexpr uint16_t kGattMaxPages = 4;        // safety bound on discover_gatt page iteration
constexpr uint32_t kWifiScanOverheadMs = 500;  // driver start-up on top of the per-channel dwell
constexpr uint32_t kMinBleScanMs = 100;
constexpr const char* kRemoteReadRefusal = "characteristic not readable by remote job (policy is not public)";

void record_event(OpResult& res, const char* evid) {
  if (res.event_count < cfg::kMaxResultEventIds) {
    copy_bounded(res.event_ids[res.event_count], cfg::kIdBufLen, evid);
    ++res.event_count;
  }
}

size_t minsz(size_t a, size_t b) { return a < b ? a : b; }

const char* policy_str(lab::Policy p) {
  switch (p) {
    case lab::Policy::Public:            return "public";
    case lab::Policy::LinkEncryption:    return "link_encryption";
    case lab::Policy::LinkAuthentication:return "link_authentication";
    case lab::Policy::AppAuthorization:  return "app_authorization";
    case lab::Policy::None:              return "none";
  }
  return "none";
}

void write_security(JsonWriter& w, const char* key, const SecurityState& s) {
  w.begin_object(key);
  w.tribool("encrypted", s.encrypted);
  w.tribool("authenticated_pairing", s.authenticated_pairing);
  w.tribool("application_authorized", s.application_authorized);
  w.end_object();
}

// ---- job limits and deadline ---------------------------------------------------------------------

// Which limit actually changed what the operation did; recorded in the summary event.
struct Applied {
  bool max_results = false;
  bool max_duration = false;
  bool max_bytes = false;
  bool job_deadline = false;
};

constexpr uint32_t kNoLimit = 0xFFFFFFFFu;

// Milliseconds left before the job's deadline on the uptime clock; kNoLimit without a job, 0 once passed.
uint32_t job_remaining_ms(const OpContext& ctx) {
  if (ctx.job == nullptr) return kNoLimit;
  const uint64_t deadline = ctx.job->received_ms + ctx.job->deadline_ms;
  const uint64_t now = timesync::uptime_ms();
  if (now >= deadline) return 0;
  const uint64_t left = deadline - now;
  return left >= kNoLimit ? kNoLimit - 1 : static_cast<uint32_t>(left);
}

bool job_expired(const OpContext& ctx) { return ctx.job != nullptr && job_remaining_ms(ctx) == 0; }

// Smallest of the requested duration (0 = none), the job's max_duration_ms (0 = none) and the time left
// before the job deadline. 0 = no bound at all.
uint32_t effective_duration_ms(const OpContext& ctx, uint32_t requested, Applied& applied) {
  uint32_t eff = requested;
  if (ctx.job != nullptr) {
    const uint32_t lim = ctx.job->limits.max_duration_ms;
    if (lim != 0 && (eff == 0 || lim < eff)) {
      eff = lim;
      applied.max_duration = true;
    }
    const uint32_t left = job_remaining_ms(ctx);
    if (left != kNoLimit && (eff == 0 || left < eff)) {
      eff = left;
      applied.job_deadline = true;
    }
  }
  return eff;
}

// null for an operation chosen at the device (no job, so no limits); otherwise the limits received and
// which of them (or the job deadline) actually changed what the operation did.
void write_job_limits(JsonWriter& w, const OpContext& ctx, const Applied& a) {
  if (ctx.job == nullptr) {
    w.null("job_limits");
    return;
  }
  w.begin_object("job_limits");
  const JobLimits* l = &ctx.job->limits;
  if (l->max_results != 0) w.unum("max_results", l->max_results); else w.null("max_results");
  if (l->max_duration_ms != 0) w.unum("max_duration_ms", l->max_duration_ms); else w.null("max_duration_ms");
  if (l->max_bytes != 0) w.unum("max_bytes", l->max_bytes); else w.null("max_bytes");
  w.begin_array("applied");
  if (a.max_results) w.str_value("max_results");
  if (a.max_duration) w.str_value("max_duration_ms");
  if (a.max_bytes) w.str_value("max_bytes");
  if (a.job_deadline) w.str_value("job_deadline");
  w.end_array();
  w.end_object();
}

// Read-only operations: a partial result is still a result. UnknownOutcome stays reserved for transport loss
// after a request that could have had an effect, which none of these operations can have.
JobOutcome outcome_for(EventStatus s) {
  switch (s) {
    case EventStatus::Success:        return JobOutcome::Succeeded;
    case EventStatus::Partial:        return JobOutcome::Succeeded;
    case EventStatus::Cancelled:      return JobOutcome::Cancelled;
    case EventStatus::UnknownOutcome: return JobOutcome::UnknownOutcome;
    case EventStatus::Unsupported:    return JobOutcome::Unsupported;
    default:                          return JobOutcome::Failed;
  }
}

EventStatus partial_if_success(EventStatus s) { return s == EventStatus::Success ? EventStatus::Partial : s; }

void note_lost_events(OpResult& res, size_t lost) {
  if (lost == 0) return;
  std::snprintf(res.error, sizeof(res.error), "%u evidence event(s) could not be stored", static_cast<unsigned>(lost));
}

// ---- paged observation events ----------------------------------------------------------------------

// Writes elements 0..count-1 as array `array_key` in as many events as the size budget needs.
// extra(w, page, first_index, more) adds page fields before the array. Returns how many events could
// not be committed (their elements are lost; the caller must not report the result complete).
template <typename WriteItem, typename ExtraFields>
size_t write_pages(char* scratch, const EventHeader& h, const char* array_key, size_t count, size_t pages_planned,
                   WriteItem&& item, ExtraFields&& extra, OpResult& res) {
  size_t i = 0;
  size_t page = 0;
  size_t failed = 0;
  size_t dropped = 0;
  while (i < count) {
    JsonWriter& w = g_evidence.begin_event(h, scratch, kEventScratchBytes);
    extra(w, page, i, page + 1 < pages_planned);
    w.begin_array(array_key);
    const size_t before = i;
    i = fill_array_page(w, scratch, kPageArrayBytes, i, count, item, dropped);
    w.end_array();
    char evid[cfg::kIdBufLen];
    if (g_evidence.commit_event(evid)) {
      record_event(res, evid);
    } else {
      ++failed;
    }
    ++page;
    if (i == before) break;  // writer unusable: stop instead of looping
  }
  if (i < count) failed += (count - i);
  return failed + dropped;
}

void write_simple_error_event(Operation op, Transport tr, const char* target_id, const char* reason,
                              OpResult& res, EventStatus status = EventStatus::Error, int candidates = -1) {
  char scratch[1024];
  EventHeader h{op, tr, status, SourceMode::Live, target_id};
  JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
  w.str("error", reason);
  if (candidates >= 0) w.unum("candidates", static_cast<uint64_t>(candidates));
  char evid[cfg::kIdBufLen];
  if (g_evidence.commit_event(evid)) record_event(res, evid);
}

// ---- target resolution (fail closed) -----------------------------------------------------------------

struct Resolved {
  EnrolledTarget target;
  TargetMatch how = TargetMatch::EnrolledAddress;
  size_t candidates = 0;
};

// An enrolled address is the only candidate. Otherwise exactly one connectable device of the latest
// discovery must advertise the enrolled service UUID or the exact name hint; two or more refuse.
ResolveResult resolve_target(const EnrolledTarget& t, Resolved& out) {
  out.target = t;
  TargetResolver r(t);
  if (!t.has_address) {
    size_t start = 0;
    for (;;) {
      BleAdvObservation buf[4];
      BleSummary s;
      const size_t got = g_results.ble_rows(start, buf, 4, s);
      for (size_t i = 0; i < got; ++i) r.add(buf[i]);
      if (got < 4) break;
      start += got;
    }
  }
  const TargetResolution x = r.finish();
  out.how = x.rule;
  out.candidates = x.candidates;
  if (x.result == ResolveResult::Resolved) {
    std::memcpy(out.target.address, x.address, 6);
    out.target.address_type = x.address_type;
    out.target.has_address = true;
  }
  return x.result;
}

// "connection" object of the evidence: how the target was matched and what the identity check saw.
// Name and service-UUID matching are hints for choosing a device to dial, never identity; the identity
// check reads the profile's public device_info value (a consistency check, not authentication).
void write_connection(JsonWriter& w, const Resolved& rs) {
  const ConnectedTargetInfo& ci = g_ble.connected_info();
  w.begin_object("connection");
  w.str("matched_by", to_string(rs.how));
  w.boolean("match_is_hint", rs.how != TargetMatch::EnrolledAddress);
  w.unum("candidates", rs.candidates);
  char mac[18];
  format_mac(rs.target.address, mac, sizeof(mac));
  w.str("address", mac);
  w.str("address_type", to_string(rs.target.address_type));
  w.unum("mtu", ci.mtu);
  w.tribool("identity_ok", ci.identity_ok);
  w.str("identity_note", ci.identity_note);
  w.str("identity_basis", "public_device_info");  // a consistency check on a public value, not authentication
  w.boolean("identity_read_attempted", ci.identity_status != -1);
  w.num("identity_read_status", ci.identity_status);
  w.unum("device_info_length", ci.identity_len);
  w.hex("device_info_hex", ci.identity_value, minsz(ci.identity_len, sizeof(ci.identity_value)));
  w.end_object();
}

// Event for a connect() that did not reach a verified link (mismatch, unreadable, timeout, cancel, error).
void write_connect_failure(Operation op, const Resolved& rs, const BleOpStatus& cs, OpResult& res) {
  char scratch[kEventScratchBytes];
  EventHeader h{op, Transport::Ble, cs.status, SourceMode::Live, rs.target.target_id};
  JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
  w.str("profile_id", rs.target.profile_id[0] != '\0' ? rs.target.profile_id : lab::kProfileId);
  w.str("error", cs.detail);
  w.num("nimble_rc", cs.nimble_rc);
  write_connection(w, rs);
  write_security(w, "security_state", cs.after);
  w.str("enumeration", "not performed: identity is verified before any service is enumerated or read");
  char evid[cfg::kIdBufLen];
  if (g_evidence.commit_event(evid)) record_event(res, evid);
}

// ---- Wi-Fi evidence ----------------------------------------------------------------------------

// Returns the number of observations/events that could not be stored. `status` may be downgraded.
size_t write_wifi_events(const OpContext& ctx, const WifiSurveyConfig& requested, const WifiSurveyConfig& c,
                         const WifiSurveyResult& r, const Applied& applied, EventStatus& status, OpResult& res) {
  char scratch[kEventScratchBytes];
  auto item = [&r](JsonWriter& w, size_t i) { write_ap_json(w, r.aps[i]); };
  const PagePlan plan = plan_pages(scratch, kPageArrayBytes, r.count, item);
  const bool complete = r.complete && plan.dropped == 0;
  if (!complete) status = partial_if_success(status);

  {
    EventHeader h{Operation::WifiSurvey, Transport::Wifi, status, SourceMode::Live, nullptr};
    JsonWriter& w = g_evidence.begin_event(h, scratch, kEventScratchBytes);
    w.str("room_label", (ctx.room_label != nullptr && ctx.room_label[0] != '\0') ? ctx.room_label : nullptr);
    w.begin_object("scan");
    w.str("mode", c.passive ? "passive" : "active");
    w.boolean("show_hidden", c.show_hidden);
    w.unum("max_ms_per_channel", c.ms_per_channel);
    w.unum("requested_ms_per_channel", requested.ms_per_channel);
    w.num("channel", c.channel);
    // Country advertised by a neighbour AP, not this device's configured regulatory domain.
    w.str("observed_ap_country", r.observed_ap_country[0] != '\0' ? r.observed_ap_country : nullptr);
    w.end_object();
    w.unum("driver_count", r.driver_count);
    w.unum("observation_count", r.count);
    w.unum("overflow_count", r.overflow);
    w.unum("observations_dropped", plan.dropped);
    w.unum("page_count", plan.pages);
    w.boolean("complete", complete);
    w.boolean("cancelled", r.cancelled);
    w.boolean("timed_out", r.timed_out);
    w.boolean("limit_hit", r.limit_hit);
    write_job_limits(w, ctx, applied);
    char evid[cfg::kIdBufLen];
    if (g_evidence.commit_event(evid)) record_event(res, evid);
  }
  EventHeader h{Operation::WifiSurvey, Transport::Wifi, status, SourceMode::Live, nullptr};
  auto extra = [](JsonWriter& w, size_t page, size_t first, bool more) {
    w.unum("page_index", page);
    w.unum("first_index", first);
    w.boolean("more_pages", more);
  };
  return plan.dropped + write_pages(scratch, h, "observations", r.count, plan.pages, item, extra, res);
}

// ---- BLE discovery evidence --------------------------------------------------------------------

size_t write_ble_events(const OpContext& ctx, const BleDiscoveryConfig& requested, const BleDiscoveryConfig& c,
                        const BleDiscoverySummary& sum, const Applied& applied, EventStatus& status, OpResult& res) {
  char scratch[kEventScratchBytes];
  // Rows are read straight from the explorer's frozen working set, one at a time.
  auto item = [](JsonWriter& w, size_t i) {
    BleAdvObservation o;
    if (g_ble.discovery_rows(i, &o, 1) == 1) {
      write_adv_json(w, o);
    } else {
      w.raw_value("null");  // never expected: the row count was taken from the same frozen set
    }
  };
  const PagePlan plan = plan_pages(scratch, kPageArrayBytes, sum.count, item);
  const bool complete = sum.complete && plan.dropped == 0;
  if (!complete) status = partial_if_success(status);

  {
    EventHeader h{Operation::BleDiscover, Transport::Ble, status, SourceMode::Live, nullptr};
    JsonWriter& w = g_evidence.begin_event(h, scratch, kEventScratchBytes);
    w.begin_object("scan");
    w.str("mode", c.active ? "active" : "passive");
    w.unum("interval_ms", sum.interval_ms);
    w.unum("window_ms", sum.window_ms);
    w.unum("duration_ms", sum.duration_ms);
    w.unum("requested_duration_ms", requested.duration_ms);
    w.unum("scheduled_duration_ms", c.duration_ms);
    w.end_object();
    w.unum("observation_count", sum.count);
    w.unum("overflow_count", sum.overflow);
    w.unum("malformed_count", sum.malformed);
    w.unum("callbacks", sum.callbacks);
    w.unum("dropped_in_callback", sum.dropped_in_callback);
    w.unum("observations_dropped", plan.dropped);
    w.unum("page_count", plan.pages);
    w.boolean("complete", complete);
    w.boolean("cancelled", sum.cancelled);
    w.boolean("limit_hit", sum.limit_hit);
    write_job_limits(w, ctx, applied);
    char evid[cfg::kIdBufLen];
    if (g_evidence.commit_event(evid)) record_event(res, evid);
  }
  EventHeader h{Operation::BleDiscover, Transport::Ble, status, SourceMode::Live, nullptr};
  auto extra = [](JsonWriter& w, size_t page, size_t first, bool more) {
    w.unum("page_index", page);
    w.unum("first_index", first);
    w.boolean("more_pages", more);
  };
  return plan.dropped + write_pages(scratch, h, "observations", sum.count, plan.pages, item, extra, res);
}

// ---- GATT evidence -----------------------------------------------------------------------------

void write_gatt_summary(const OpContext& ctx, const Resolved& rs, const BleOpStatus& ds, EventStatus status,
                        const GattPage& first, const Applied& applied, OpResult& res) {
  char scratch[kEventScratchBytes];
  EventHeader h{Operation::BleInspectGatt, Transport::Ble, status, SourceMode::Live, rs.target.target_id};
  JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
  w.str("profile_id", rs.target.profile_id[0] != '\0' ? rs.target.profile_id : lab::kProfileId);
  write_connection(w, rs);
  write_security(w, "security_state", g_ble.security_state());
  w.unum("total_entries", first.total_seen);
  w.boolean("complete", first.complete);
  w.str("enumeration", ds.detail);
  write_job_limits(w, ctx, applied);
  char evid[cfg::kIdBufLen];
  if (g_evidence.commit_event(evid)) record_event(res, evid);
}

size_t write_gatt_page(const char* target_id, const GattPage& gp, EventStatus status, OpResult& res) {
  char scratch[kEventScratchBytes];
  auto item = [&gp](JsonWriter& w, size_t i) { write_gatt_entry_json(w, gp.entries[i]); };
  const PagePlan plan = plan_pages(scratch, kPageArrayBytes, gp.count, item);
  EventHeader h{Operation::BleInspectGatt, Transport::Ble, status, SourceMode::Live, target_id};
  auto extra = [&gp](JsonWriter& w, size_t page, size_t first, bool more) {
    w.unum("page_index", gp.page_index);
    w.unum("part", page);
    w.unum("entry_offset", first);
    w.unum("total_seen", gp.total_seen);
    w.boolean("complete", gp.complete);
    w.boolean("more_pages", more || gp.more_pages);
  };
  return plan.dropped + write_pages(scratch, h, "entries", gp.count, plan.pages, item, extra, res);
}

// Common start of the connected operations. On refusal it has written the evidence event, released the
// radio phase and filled `res`; returns false.
bool open_verified_link(const OpContext& ctx, Operation op, RadioPhase phase, Resolved& rs, Applied& applied,
                        OpResult& res) {
  const ResolveResult rr = resolve_target(*ctx.target, rs);
  if (rr != ResolveResult::Resolved) {
    char reason[96];
    if (rr == ResolveResult::Ambiguous) {
      std::snprintf(reason, sizeof(reason), "ambiguous target: %u devices match; enrol an address",
                    static_cast<unsigned>(rs.candidates));
    } else {
      std::snprintf(reason, sizeof(reason), "target not observed - run discovery first");
    }
    write_simple_error_event(op, Transport::Ble, ctx.target->target_id, reason, res, EventStatus::Error,
                             static_cast<int>(rs.candidates));
    g_radio.release(phase);
    copy_bounded(res.error, sizeof(res.error), reason);
    app_post(AppEventType::OperationFinished, static_cast<uint32_t>(op), static_cast<uint32_t>(EventStatus::Error),
             rr == ResolveResult::Ambiguous ? "ambiguous target" : "target not observed");
    return false;
  }
  if (job_expired(ctx)) {
    write_simple_error_event(op, Transport::Ble, rs.target.target_id, "job deadline passed before connect", res,
                             EventStatus::Timeout);
    g_radio.release(phase);
    copy_bounded(res.error, sizeof(res.error), "job deadline passed");
    res.status = EventStatus::Timeout;
    res.outcome = JobOutcome::Expired;
    app_post(AppEventType::OperationFinished, static_cast<uint32_t>(op), static_cast<uint32_t>(EventStatus::Timeout),
             "deadline passed");
    return false;
  }

  BleOpLimits bl;
  bl.max_duration_ms = effective_duration_ms(ctx, 0, applied);
  if (ctx.job != nullptr && ctx.job->limits.max_results != 0) {
    bl.max_entries = ctx.job->limits.max_results;
  }
  g_ble.set_limits(bl);

  const BleOpStatus cs = g_ble.connect(rs.target, cfg::kBleConnectTimeoutMs);
  if (cs.status == EventStatus::Success) return true;

  write_connect_failure(op, rs, cs, res);
  g_ble.disconnect();  // connect() already closed an unverified link; this is a no-op safety net
  g_ble.set_limits(BleOpLimits{});
  g_radio.release(phase);
  copy_bounded(res.error, sizeof(res.error), cs.detail);
  res.status = cs.status;
  res.outcome = (cs.status == EventStatus::Timeout && job_expired(ctx)) ? JobOutcome::Expired : outcome_for(cs.status);
  app_post(AppEventType::OperationFinished, static_cast<uint32_t>(op), static_cast<uint32_t>(cs.status),
           cs.status == EventStatus::Denied ? "identity mismatch" : "connect failed");
  return false;
}

}  // namespace

// ================================================================================================

OpResult op_wifi_survey(const OpContext& ctx, const WifiSurveyConfig& c) {
  OpResult res{};
  res.status = EventStatus::Error;
  res.outcome = JobOutcome::Failed;
  res.event_count = 0;
  res.error[0] = '\0';

  // Effective configuration under the job's limits.
  WifiSurveyConfig cfg_eff = c;
  Applied applied;
  if (ctx.job != nullptr && ctx.job->limits.max_results != 0) {
    const uint8_t lim = ctx.job->limits.max_results > 255 ? 255 : static_cast<uint8_t>(ctx.job->limits.max_results);
    if (cfg_eff.max_results == 0 || lim < cfg_eff.max_results) cfg_eff.max_results = lim;
  }
  const uint32_t chans = (cfg_eff.channel == 0) ? 14 : 1;
  const uint32_t bound = effective_duration_ms(ctx, 0, applied);
  if (bound != 0) {
    const uint32_t avail = bound > kWifiScanOverheadMs ? bound - kWifiScanOverheadMs : 0;
    const uint32_t per = avail / chans;
    if (per < 20) {
      write_simple_error_event(Operation::WifiSurvey, Transport::Wifi, nullptr,
                               "time limit too small for a scan", res, EventStatus::Timeout);
      copy_bounded(res.error, sizeof(res.error), "time limit too small for a scan");
      res.status = EventStatus::Timeout;
      res.outcome = job_expired(ctx) ? JobOutcome::Expired : JobOutcome::Failed;
      return res;
    }
    if (per < cfg_eff.ms_per_channel) cfg_eff.ms_per_channel = per;
    cfg_eff.max_duration_ms = bound;
  }

  if (!g_radio.acquire(RadioPhase::WifiSurvey)) {
    copy_bounded(res.error, sizeof(res.error), "radio busy");
    return res;
  }
  app_post(AppEventType::OperationStarted, static_cast<uint32_t>(Operation::WifiSurvey), 0, "wifi survey");

  if (!g_wifi_survey.start(cfg_eff)) {
    write_simple_error_event(Operation::WifiSurvey, Transport::Wifi, nullptr, "scan start failed", res);
    g_radio.release(RadioPhase::WifiSurvey);
    copy_bounded(res.error, sizeof(res.error), "scan start failed");
    app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::WifiSurvey),
             static_cast<uint32_t>(EventStatus::Error), "wifi start failed");
    return res;
  }

  // The driver reports APs only when the scan has finished, so there is no running count to show.
  WifiSurveyResult r{};
  PollState ps = PollState::Running;
  bool expired = false;
  for (;;) {
    ps = g_wifi_survey.poll(r);
    if (ps != PollState::Running) break;
    if (g_radio.cancel_requested()) {
      g_wifi_survey.cancel();
    } else if (!expired && job_expired(ctx)) {
      expired = true;
      g_wifi_survey.cancel();
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }

  EventStatus status;
  if (ps == PollState::Done) {
    status = EventStatus::Success;  // downgraded to Partial below when anything was dropped or limited
  } else if (ps == PollState::Cancelled) {
    status = expired ? EventStatus::Timeout : EventStatus::Cancelled;
  } else {
    status = r.timed_out ? EventStatus::Timeout : EventStatus::Error;
  }
  applied.max_results = r.limit_hit;
  const size_t lost = write_wifi_events(ctx, c, cfg_eff, r, applied, status, res);
  if (lost > 0) status = partial_if_success(status);
  const char* room = (ctx.room_label != nullptr) ? ctx.room_label : "";
  g_results.put_wifi(r, room);
  g_evidence.append_wifi_csv(r, room);
  g_radio.release(RadioPhase::WifiSurvey);

  app_post(AppEventType::OperationProgress, static_cast<uint32_t>(Operation::WifiSurvey), r.count);
  app_post(AppEventType::ResultsUpdated, static_cast<uint32_t>(ResultKind::Wifi));
  app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::WifiSurvey),
           static_cast<uint32_t>(status), "wifi survey done");
  res.status = status;
  res.outcome = (status == EventStatus::Timeout && expired) ? JobOutcome::Expired : outcome_for(status);
  note_lost_events(res, lost);
  return res;
}

OpResult op_ble_discover(const OpContext& ctx, const BleDiscoveryConfig& c) {
  OpResult res{};
  res.status = EventStatus::Error;
  res.outcome = JobOutcome::Failed;
  res.event_count = 0;
  res.error[0] = '\0';

  BleDiscoveryConfig cfg_eff = c;
  Applied applied;
  if (ctx.job != nullptr && ctx.job->limits.max_results != 0) {
    const uint8_t lim = ctx.job->limits.max_results > 255 ? 255 : static_cast<uint8_t>(ctx.job->limits.max_results);
    if (cfg_eff.max_results == 0 || lim < cfg_eff.max_results) cfg_eff.max_results = lim;
  }
  const uint32_t bound = effective_duration_ms(ctx, c.duration_ms, applied);
  if (bound < kMinBleScanMs) {
    write_simple_error_event(Operation::BleDiscover, Transport::Ble, nullptr, "time limit too small for a scan", res,
                             EventStatus::Timeout);
    copy_bounded(res.error, sizeof(res.error), "time limit too small for a scan");
    res.status = EventStatus::Timeout;
    res.outcome = job_expired(ctx) ? JobOutcome::Expired : JobOutcome::Failed;
    return res;
  }
  cfg_eff.duration_ms = bound;

  if (!g_radio.acquire(RadioPhase::BleDiscovery)) {
    copy_bounded(res.error, sizeof(res.error), "radio busy");
    return res;
  }
  app_post(AppEventType::OperationStarted, static_cast<uint32_t>(Operation::BleDiscover), 0, "ble discovery");

  if (!g_ble.start_discovery(cfg_eff)) {
    write_simple_error_event(Operation::BleDiscover, Transport::Ble, nullptr, "discovery start failed", res);
    g_radio.release(RadioPhase::BleDiscovery);
    copy_bounded(res.error, sizeof(res.error), "discovery start failed");
    app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::BleDiscover),
             static_cast<uint32_t>(EventStatus::Error), "ble start failed");
    return res;
  }

  // The rows stay in BleExplorer's working set: only this small summary lives on the 16 KiB worker stack.
  BleDiscoverySummary sum{};
  PollState ps = PollState::Running;
  uint32_t last_progress = 0;
  bool expired = false;
  for (;;) {
    ps = g_ble.poll_discovery_summary(sum);
    if (ps != PollState::Running) break;
    if (!expired && job_expired(ctx)) {
      expired = true;
      g_ble.stop_discovery();
    }
    const uint32_t now = millis();
    if (now - last_progress > 500) {
      app_post(AppEventType::OperationProgress, static_cast<uint32_t>(Operation::BleDiscover), sum.count);
      last_progress = now;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }

  EventStatus status;
  if (ps == PollState::Cancelled) {
    status = EventStatus::Cancelled;
  } else if (expired) {
    status = EventStatus::Timeout;
  } else {
    status = sum.complete ? EventStatus::Success : EventStatus::Partial;
  }
  applied.max_results = sum.limit_hit;
  const size_t lost = write_ble_events(ctx, c, cfg_eff, sum, applied, status, res);
  if (lost > 0) status = partial_if_success(status);
  g_results.put_ble_from_explorer(sum);
  g_radio.release(RadioPhase::BleDiscovery);

  app_post(AppEventType::OperationProgress, static_cast<uint32_t>(Operation::BleDiscover), sum.count);
  app_post(AppEventType::ResultsUpdated, static_cast<uint32_t>(ResultKind::Ble));
  app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::BleDiscover),
           static_cast<uint32_t>(status), "ble discovery done");
  res.status = status;
  res.outcome = (status == EventStatus::Timeout && expired) ? JobOutcome::Expired : outcome_for(status);
  note_lost_events(res, lost);
  return res;
}

OpResult op_ble_inspect_gatt(const OpContext& ctx) {
  OpResult res{};
  res.status = EventStatus::Error;
  res.outcome = JobOutcome::Failed;
  res.event_count = 0;
  res.error[0] = '\0';

  if (ctx.target == nullptr) {
    copy_bounded(res.error, sizeof(res.error), "target required");
    return res;
  }
  if (!g_radio.acquire(RadioPhase::BleSession)) {
    copy_bounded(res.error, sizeof(res.error), "radio busy");
    return res;
  }
  app_post(AppEventType::OperationStarted, static_cast<uint32_t>(Operation::BleInspectGatt), 0, "inspect gatt");

  Resolved rs;
  Applied applied;
  if (!open_verified_link(ctx, Operation::BleInspectGatt, RadioPhase::BleSession, rs, applied, res)) return res;

  // The first call enumerates (identity is already verified); later calls copy out further pages.
  GattPage gp;
  const BleOpStatus ds = g_ble.discover_gatt(0, gp);
  EventStatus gstat = ds.status;
  size_t lost = 0;
  if (gstat == EventStatus::Success || gstat == EventStatus::Partial || gstat == EventStatus::Cancelled ||
      gstat == EventStatus::Timeout) {
    if (gstat == EventStatus::Partial && ctx.job != nullptr && ctx.job->limits.max_results != 0 &&
        gp.total_seen >= ctx.job->limits.max_results) {
      applied.max_results = true;
    }
    write_gatt_summary(ctx, rs, ds, gstat, gp, applied, res);
    uint16_t page = 0;
    for (;;) {
      g_results.put_gatt(rs.target.target_id, gp);
      lost += write_gatt_page(rs.target.target_id, gp, gstat, res);
      if (!gp.more_pages || ++page > kGattMaxPages) break;
      const BleOpStatus next = g_ble.discover_gatt(page, gp);
      if (next.status == EventStatus::Error) {
        gstat = partial_if_success(gstat);
        break;
      }
    }
  } else {
    write_simple_error_event(Operation::BleInspectGatt, Transport::Ble, rs.target.target_id, ds.detail, res);
    copy_bounded(res.error, sizeof(res.error), ds.detail);
  }
  if (lost > 0) gstat = partial_if_success(gstat);

  g_ble.disconnect();
  g_ble.set_limits(BleOpLimits{});
  g_radio.release(RadioPhase::BleSession);
  app_post(AppEventType::ResultsUpdated, static_cast<uint32_t>(ResultKind::Gatt));
  app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::BleInspectGatt),
           static_cast<uint32_t>(gstat), "inspect gatt done");
  res.status = gstat;
  res.outcome = (gstat == EventStatus::Timeout && job_expired(ctx)) ? JobOutcome::Expired : outcome_for(gstat);
  note_lost_events(res, lost);
  return res;
}

OpResult op_ble_read_profile_value(const OpContext& ctx, const char* characteristic_id) {
  OpResult res{};
  res.status = EventStatus::Error;
  res.outcome = JobOutcome::Failed;
  res.event_count = 0;
  res.error[0] = '\0';

  const lab::CharDef* def = lab::find_char(characteristic_id);
  if (def == nullptr) {
    copy_bounded(res.error, sizeof(res.error), "unknown characteristic");
    return res;
  }
  if (ctx.target == nullptr) {
    copy_bounded(res.error, sizeof(res.error), "target required");
    return res;
  }

  // A remote job may read only public, side-effect-free characteristics. Protected ones belong to the
  // gated access-policy assertions, and reading app_auth would replace the fixture's outstanding challenge.
  // Refused before the radio is touched; a read chosen at the device (no job) is not restricted.
  if (ctx.job != nullptr && !lab_remote_read_allowed(characteristic_id)) {
    char scratch[1024];
    EventHeader h{Operation::BleReadProfileValue, Transport::Ble, EventStatus::Denied, SourceMode::Live,
                  ctx.target->target_id};
    JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
    w.str("profile_id", ctx.target->profile_id[0] != '\0' ? ctx.target->profile_id : lab::kProfileId);
    w.str("characteristic_id", characteristic_id);
    w.str("read_policy", policy_str(def->read));
    w.str("error", kRemoteReadRefusal);
    w.boolean("attempted", false);
    char evid[cfg::kIdBufLen];
    if (g_evidence.commit_event(evid)) record_event(res, evid);
    copy_bounded(res.error, sizeof(res.error), kRemoteReadRefusal);
    res.status = EventStatus::Denied;
    res.outcome = JobOutcome::Failed;
    return res;
  }

  if (!g_radio.acquire(RadioPhase::BleSession)) {
    copy_bounded(res.error, sizeof(res.error), "radio busy");
    return res;
  }
  app_post(AppEventType::OperationStarted, static_cast<uint32_t>(Operation::BleReadProfileValue), 0, "read value");

  Resolved rs;
  Applied applied;
  if (!open_verified_link(ctx, Operation::BleReadProfileValue, RadioPhase::BleSession, rs, applied, res)) return res;

  uint8_t svc_be[16];
  uint8_t chr_be[16];
  parse_uuid128(lab::kServiceUuid, svc_be);
  parse_uuid128(def->uuid, chr_be);

  uint8_t buf[256];
  size_t cap = sizeof(buf);
  if (ctx.job != nullptr && ctx.job->limits.max_bytes != 0 && ctx.job->limits.max_bytes < cap) {
    cap = ctx.job->limits.max_bytes;
  }
  size_t n = 0;
  size_t full = 0;
  const BleOpStatus rs_read = g_ble.read_by_uuid(svc_be, chr_be, buf, cap, n, &full);
  const bool truncated = full > n;
  if (truncated && cap < sizeof(buf)) applied.max_bytes = true;

  const bool public_read = (def->read == lab::Policy::Public);
  EventStatus status = rs_read.status;
  if (status == EventStatus::Success && truncated) status = EventStatus::Partial;

  // Evidence event.
  {
    char scratch[kEventScratchBytes];
    EventHeader h{Operation::BleReadProfileValue, Transport::Ble, status, SourceMode::Live, rs.target.target_id};
    JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
    w.str("profile_id", rs.target.profile_id[0] != '\0' ? rs.target.profile_id : lab::kProfileId);
    w.str("characteristic_id", characteristic_id);
    char u[cfg::kUuidStrLen + 1];
    format_uuid128(chr_be, u, sizeof(u));
    w.str("characteristic_uuid", u);
    w.str("read_policy", policy_str(def->read));
    w.null("app_status");       // ATT application status is not exposed by the stack on reads
    w.null("raw_att_status");
    write_security(w, "security_state", rs_read.after);
    write_security(w, "security_before", rs_read.before);
    write_security(w, "security_after", rs_read.after);
    w.boolean("security_transition", rs_read.state_changed);
    w.unum("value_length", full);
    if (full > 0 && !truncated) {
      uint8_t digest[32];
      char hexd[65];
      Sha256 sh;
      sh.update(buf, n);
      sh.finish(digest);
      Sha256::to_hex(digest, hexd);
      w.str("value_sha256", hexd);
    } else {
      w.null("value_sha256");  // empty read, or the full value was not retained (no hash of a partial value)
    }
    w.boolean("value_truncated", truncated);
    if (public_read && n > 0) w.hex("value_hex", buf, n);  // only public-policy reads expose the value
    w.str("detail", rs_read.detail);
    write_connection(w, rs);
    write_job_limits(w, ctx, applied);
    char evid[cfg::kIdBufLen];
    if (g_evidence.commit_event(evid)) {
      record_event(res, evid);
    } else {
      status = partial_if_success(status);  // the result is valid but its evidence was not stored
      note_lost_events(res, 1);
    }
  }

  // UI view.
  ReadResultView v{};
  copy_bounded(v.target_id, sizeof(v.target_id), rs.target.target_id);
  copy_bounded(v.characteristic_id, sizeof(v.characteristic_id), characteristic_id);
  v.status = status;
  v.value_length = static_cast<uint16_t>(full > 0xFFFF ? 0xFFFF : full);
  v.sec = rs_read.after;
  v.preview_withheld = !public_read;
  if (public_read) {
    v.preview_len = static_cast<uint8_t>(minsz(n, sizeof(v.preview)));
    std::memcpy(v.preview, buf, v.preview_len);
  } else {
    v.preview_len = 0;
  }
  if (res.event_count > 0) copy_bounded(v.event_id, sizeof(v.event_id), res.event_ids[res.event_count - 1]);
  g_results.put_read(v);

  g_ble.disconnect();
  g_ble.set_limits(BleOpLimits{});
  g_radio.release(RadioPhase::BleSession);
  app_post(AppEventType::ResultsUpdated, static_cast<uint32_t>(ResultKind::Read));
  app_post(AppEventType::OperationFinished, static_cast<uint32_t>(Operation::BleReadProfileValue),
           static_cast<uint32_t>(status), "read value done");
  res.status = status;
  if (status == EventStatus::Inconclusive) {
    // An empty value is a completed read-only request whose ATT status the stack does not expose.
    res.outcome = JobOutcome::Succeeded;
  } else if (status == EventStatus::Timeout && job_expired(ctx)) {
    res.outcome = JobOutcome::Expired;
  } else {
    res.outcome = outcome_for(status);
  }
  return res;
}

OpResult op_unsupported(const OpContext& ctx, Operation op, const char* reason) {
  OpResult res{};
  res.status = EventStatus::Unsupported;
  res.outcome = JobOutcome::Unsupported;
  res.event_count = 0;
  const char* r = (reason != nullptr) ? reason : "operation not supported in this firmware";
  copy_bounded(res.error, sizeof(res.error), r);

  char scratch[cfg::kMaxEventBytes - kEventEnvelopeReserve];
  EventHeader h{op, Transport::None, EventStatus::Unsupported, SourceMode::Live,
                (ctx.target != nullptr) ? ctx.target->target_id : nullptr};
  JsonWriter& w = g_evidence.begin_event(h, scratch, sizeof(scratch));
  w.str("requested_operation", to_string(op));
  w.str("reason", r);
  char evid[cfg::kIdBufLen];
  if (g_evidence.commit_event(evid)) record_event(res, evid);
  return res;
}

// ---- Argument parsing (ArduinoJson 7, bounded) -------------------------------------------------

bool parse_wifi_survey_args(const JobSpec& j, WifiSurveyConfig& out, char* err, size_t err_len) {
  out = WifiSurveyConfig{};
  if (j.args_len == 0) return true;
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, j.args_json, j.args_len);
  if (e) {
    std::snprintf(err, err_len, "args not json: %s", e.c_str());
    return false;
  }
  if (!doc.isNull() && !doc.is<JsonObject>()) {
    std::snprintf(err, err_len, "args not an object");
    return false;
  }
  auto vp = doc["passive"];
  if (!vp.isNull()) {
    if (!vp.is<bool>()) { std::snprintf(err, err_len, "passive not bool"); return false; }
    out.passive = vp.as<bool>();
  }
  auto vh = doc["show_hidden"];
  if (!vh.isNull()) {
    if (!vh.is<bool>()) { std::snprintf(err, err_len, "show_hidden not bool"); return false; }
    out.show_hidden = vh.as<bool>();
  }
  auto vm = doc["ms_per_channel"];
  if (!vm.isNull()) {
    if (!vm.is<int>()) { std::snprintf(err, err_len, "ms_per_channel not integer"); return false; }
    long x = vm.as<long>();
    if (x < 20 || x > 1500) { std::snprintf(err, err_len, "ms_per_channel out of range [20,1500]"); return false; }
    out.ms_per_channel = static_cast<uint32_t>(x);
  }
  auto vc = doc["channel"];
  if (!vc.isNull()) {
    if (!vc.is<int>()) { std::snprintf(err, err_len, "channel not integer"); return false; }
    int ch = vc.as<int>();
    if (ch < 0 || ch > 14) { std::snprintf(err, err_len, "channel out of range [0,14]"); return false; }
    out.channel = static_cast<uint8_t>(ch);
  }
  return true;
}

bool parse_ble_discover_args(const JobSpec& j, BleDiscoveryConfig& out, char* err, size_t err_len) {
  out = BleDiscoveryConfig{};
  if (j.args_len == 0) return true;
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, j.args_json, j.args_len);
  if (e) {
    std::snprintf(err, err_len, "args not json: %s", e.c_str());
    return false;
  }
  if (!doc.isNull() && !doc.is<JsonObject>()) {
    std::snprintf(err, err_len, "args not an object");
    return false;
  }
  auto vd = doc["duration_ms"];
  if (!vd.isNull()) {
    if (!vd.is<int>()) { std::snprintf(err, err_len, "duration_ms not integer"); return false; }
    long x = vd.as<long>();
    if (x < 100 || x > static_cast<long>(cfg::kBleDiscoveryMaxMs)) {
      std::snprintf(err, err_len, "duration_ms out of range [100,%lu]", static_cast<unsigned long>(cfg::kBleDiscoveryMaxMs));
      return false;
    }
    out.duration_ms = static_cast<uint32_t>(x);
  }
  auto va = doc["active"];
  if (!va.isNull()) {
    if (!va.is<bool>()) { std::snprintf(err, err_len, "active not bool"); return false; }
    out.active = va.as<bool>();
  }
  auto vi = doc["interval_ms"];
  if (!vi.isNull()) {
    if (!vi.is<int>()) { std::snprintf(err, err_len, "interval_ms not integer"); return false; }
    long x = vi.as<long>();
    if (x < 20 || x > 10240) { std::snprintf(err, err_len, "interval_ms out of range [20,10240]"); return false; }
    out.interval_ms = static_cast<uint16_t>(x);
  }
  auto vw = doc["window_ms"];
  if (!vw.isNull()) {
    if (!vw.is<int>()) { std::snprintf(err, err_len, "window_ms not integer"); return false; }
    long x = vw.as<long>();
    if (x < 20 || x > 10240) { std::snprintf(err, err_len, "window_ms out of range [20,10240]"); return false; }
    out.window_ms = static_cast<uint16_t>(x);
  }
  if (out.window_ms > out.interval_ms) {
    std::snprintf(err, err_len, "window_ms exceeds interval_ms");
    return false;
  }
  return true;
}

bool parse_ble_read_args(const JobSpec& j, char* characteristic_id, size_t id_len, char* err, size_t err_len) {
  if (j.args_len == 0) {
    std::snprintf(err, err_len, "characteristic_id required");
    return false;
  }
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, j.args_json, j.args_len);
  if (e) {
    std::snprintf(err, err_len, "args not json: %s", e.c_str());
    return false;
  }
  auto vc = doc["characteristic_id"];
  if (vc.isNull() || !vc.is<const char*>()) {
    std::snprintf(err, err_len, "characteristic_id required");
    return false;
  }
  const char* cid = vc.as<const char*>();
  if (cid == nullptr || cid[0] == '\0') {
    std::snprintf(err, err_len, "characteristic_id required");
    return false;
  }
  if (!copy_bounded(characteristic_id, id_len, cid)) {
    std::snprintf(err, err_len, "characteristic_id too long");
    return false;
  }
  return true;
}

}  // namespace oyster
