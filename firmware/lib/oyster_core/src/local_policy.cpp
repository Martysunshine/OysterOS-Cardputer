// Device-side job validation (docs/API.md job lifecycle; ADR-005). Checks run in a fixed order so the
// verdict for a given job and state is deterministic.
#include "oyster_core/local_policy.h"

#include <cstring>

#include "oyster_core/ids.h"

namespace oyster {

const char* to_string(PolicyVerdict v) {
  switch (v) {
    case PolicyVerdict::Accept: return "accept";
    case PolicyVerdict::RejectMalformed: return "reject_malformed";
    case PolicyVerdict::RejectUnknownOperation: return "reject_unknown_operation";
    case PolicyVerdict::RejectWrongDevice: return "reject_wrong_device";
    case PolicyVerdict::RejectWrongSession: return "reject_wrong_session";
    case PolicyVerdict::RejectTargetRequired: return "reject_target_required";
    case PolicyVerdict::RejectUnknownTarget: return "reject_unknown_target";
    case PolicyVerdict::RejectOperationNotAllowed: return "reject_operation_not_allowed";
    case PolicyVerdict::RejectStaleScope: return "reject_stale_scope";
    case PolicyVerdict::RejectExpired: return "reject_expired";
    case PolicyVerdict::RejectArgsTooLarge: return "reject_args_too_large";
    case PolicyVerdict::RejectLimitsExceeded: return "reject_limits_exceeded";
    case PolicyVerdict::RejectDuplicate: return "reject_duplicate";
    case PolicyVerdict::RejectNotAuthenticated: return "reject_not_authenticated";
  }
  return "reject_malformed";
}

bool ExecutedJobLedger::contains(const char* job_id) const {
  if (job_id == nullptr || job_id[0] == '\0') return false;
  for (size_t i = 0; i < count_; ++i) {
    if (std::strncmp(ids_[i], job_id, cfg::kIdBufLen) == 0) return true;
  }
  return false;
}

void ExecutedJobLedger::record(const char* job_id) {
  if (job_id == nullptr || job_id[0] == '\0' || contains(job_id)) return;
  copy_bounded(ids_[head_], cfg::kIdBufLen, job_id);  // ids are validated (<= 44 chars) before this
  head_ = (head_ + 1) % kCapacity;
  if (count_ < kCapacity) ++count_;
}

bool operation_requires_local_confirmation(Operation op) {
  return op == Operation::BleWriteLabValue || op == Operation::BleCollectDiagnostics;
}

bool operation_requires_target(Operation op) {
  switch (op) {
    case Operation::BleInspectGatt:
    case Operation::BleReadProfileValue:
    case Operation::BleObserveNotifications:
    case Operation::BleCheckAccessPolicy:
    case Operation::BleWriteLabValue:
    case Operation::BleCollectDiagnostics:
      return true;
    default:
      return false;
  }
}

bool operation_changes_target_state(Operation op) {
  return op == Operation::BleWriteLabValue || op == Operation::BleCollectDiagnostics ||
         op == Operation::BleObserveNotifications || op == Operation::BleCheckAccessPolicy;
}

namespace {

constexpr uint16_t kMaxResultsBound = 256;
constexpr uint32_t kScanDurationBoundMs = 30000;

bool is_empty(const char* s) { return s == nullptr || s[0] == '\0'; }

// Bounded equality for fixed-size id fields.
bool same_id(const char* a, const char* b) {
  if (a == nullptr || b == nullptr) return false;
  return std::strncmp(a, b, cfg::kIdBufLen) == 0;
}

PolicyDecision make(PolicyVerdict v, const JobSpec& job, const EnrolledTarget* t, const char* reason) {
  PolicyDecision d;
  d.verdict = v;
  d.requires_local_confirmation = operation_requires_local_confirmation(job.op);  // never from job flags
  d.target = t;
  d.reason = reason;
  return d;
}

const EnrolledTarget* find_target(const PolicyInput& in, const char* target_id) {
  if (in.targets == nullptr) return nullptr;
  for (size_t i = 0; i < in.target_count; ++i) {
    if (same_id(in.targets[i].target_id, target_id)) return &in.targets[i];
  }
  return nullptr;
}

}  // namespace

bool limits_within_bounds(Operation op, const JobLimits& limits) {
  if (limits.max_results > kMaxResultsBound) return false;
  if (limits.max_bytes > cfg::kMaxDiagObjectBytes) return false;
  const uint32_t duration_bound =
      (op == Operation::WifiSurvey || op == Operation::BleDiscover) ? kScanDurationBoundMs : cfg::kMaxJobDurationMs;
  if (limits.max_duration_ms > duration_bound) return false;
  return true;
}

// A job that names a target is bound to it even when the operation does not need one: the target
// must be enrolled, allow the operation and match the scope version.
PolicyDecision evaluate_job(const JobSpec& job, const PolicyInput& in, const ExecutedJobLedger& ledger) {
  // 1. Malformed ids or fields.
  if (!is_valid_job_id(job.job_id) || !is_valid_session_id(job.session_id) ||
      (!is_empty(job.device_id) && !is_valid_device_id(job.device_id)) ||
      (!is_empty(job.target_id) && !is_valid_target_id(job.target_id)) ||
      (!is_empty(job.nonce) && !is_valid_nonce(job.nonce)) || job.scope_version == 0) {
    return make(PolicyVerdict::RejectMalformed, job, nullptr, "malformed job id, session id, target id, nonce or scope");
  }
  if (operation_requires_local_confirmation(job.op) && is_empty(job.nonce)) {
    return make(PolicyVerdict::RejectMalformed, job, nullptr, "operation needs local confirmation but the job has no nonce");
  }
  // 2. Operation.
  if (!is_job_operation(job.op)) {
    return make(PolicyVerdict::RejectUnknownOperation, job, nullptr, "operation is not a supported job operation");
  }
  // 3. Device binding.
  if (!is_empty(job.device_id) && !same_id(job.device_id, in.device_id == nullptr ? "" : in.device_id)) {
    return make(PolicyVerdict::RejectWrongDevice, job, nullptr, "job is addressed to another device");
  }
  // 4. Session binding.
  if (is_empty(in.active_session_id) || !same_id(job.session_id, in.active_session_id)) {
    return make(PolicyVerdict::RejectWrongSession, job, nullptr, "job does not belong to the active session");
  }
  // 5. Authenticated connectivity.
  if (!in.backend_authenticated) {
    return make(PolicyVerdict::RejectNotAuthenticated, job, nullptr, "no authenticated backend connectivity");
  }
  // 6. Never execute a job id twice.
  if (ledger.contains(job.job_id)) {
    return make(PolicyVerdict::RejectDuplicate, job, nullptr, "job id was already started on this device");
  }
  // 7. Target resolution.
  const EnrolledTarget* target = nullptr;
  if (is_empty(job.target_id)) {
    if (operation_requires_target(job.op)) {
      return make(PolicyVerdict::RejectTargetRequired, job, nullptr, "operation requires an enrolled target");
    }
  } else {
    target = find_target(in, job.target_id);
    if (target == nullptr) {
      return make(PolicyVerdict::RejectUnknownTarget, job, nullptr, "target is not enrolled on this device");
    }
  }
  if (target != nullptr) {
    // 8. Operation allowed for the target.
    if ((target->allowed_ops_mask & op_bit(job.op)) == 0) {
      return make(PolicyVerdict::RejectOperationNotAllowed, job, target, "operation is not allowed for this target");
    }
    // 9. Scope version.
    if (job.scope_version != target->scope_version) {
      return make(PolicyVerdict::RejectStaleScope, job, target, "job scope version does not match the enrolled target");
    }
  }
  // 10. Deadline.
  if (job.deadline_ms == 0 || in.now_ms > job.received_ms + job.deadline_ms) {
    return make(PolicyVerdict::RejectExpired, job, target, "job deadline has passed");
  }
  // 11. Argument size.
  if (job.args_len > cfg::kMaxJobArgsBytes) {
    return make(PolicyVerdict::RejectArgsTooLarge, job, target, "job arguments exceed the size limit");
  }
  // 12. Limits.
  if (job.deadline_ms > cfg::kMaxJobDurationMs || !limits_within_bounds(job.op, job.limits)) {
    return make(PolicyVerdict::RejectLimitsExceeded, job, target, "job limits exceed the device bounds");
  }
  return make(PolicyVerdict::Accept, job, target, "accepted");
}

bool confirmation_valid(const JobSpec& job, const LocalConfirmation& c, uint32_t timeout_ms) {
  if (!c.approved) return false;
  if (is_empty(job.job_id) || !same_id(job.job_id, c.job_id)) return false;
  if (is_empty(job.nonce) || !same_id(job.nonce, c.nonce)) return false;
  if (c.decided_ms < job.received_ms) return false;  // decided before the job existed: not for this job
  return c.decided_ms <= job.received_ms + timeout_ms;
}

}  // namespace oyster
