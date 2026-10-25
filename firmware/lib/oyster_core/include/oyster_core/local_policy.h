// LocalPolicy: the device validates every job independently of the backend (docs/API.md, job lifecycle).
// The local-confirmation requirement is derived from the OPERATION TYPE only. A backend or model
// flag can never lower it, and a confirmation is bound to one exact job id + nonce.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster {

struct EnrolledTarget {
  char target_id[cfg::kIdBufLen];
  char profile_id[32];
  uint32_t scope_version;
  uint16_t allowed_ops_mask;  // bit (1u << static_cast<int>(Operation)) for job operations
  char advertised_name[cfg::kMaxBleNameBytes + 1];  // empty when unknown
  uint8_t service_uuid128[16];
  bool has_service_uuid;
  uint8_t address[6];
  bool has_address;
  BleAddrType address_type;
};

inline uint16_t op_bit(Operation op) { return static_cast<uint16_t>(1u << static_cast<uint8_t>(op)); }

enum class PolicyVerdict : uint8_t {
  Accept,
  RejectMalformed,          // missing/invalid ids or fields
  RejectUnknownOperation,
  RejectWrongDevice,
  RejectWrongSession,
  RejectTargetRequired,
  RejectUnknownTarget,
  RejectOperationNotAllowed,
  RejectStaleScope,
  RejectExpired,
  RejectArgsTooLarge,
  RejectLimitsExceeded,
  RejectDuplicate,          // this job id was already started on this device: never re-executed
  RejectNotAuthenticated,   // no new remote actions without authenticated backend connectivity
};
const char* to_string(PolicyVerdict v);

// Bounded memory of job ids already started on this device (ring; oldest evicted).
class ExecutedJobLedger {
 public:
  static constexpr size_t kCapacity = 32;
  bool contains(const char* job_id) const;
  void record(const char* job_id);
  size_t size() const { return count_; }
  void clear() { count_ = 0; head_ = 0; }

 private:
  char ids_[kCapacity][cfg::kIdBufLen] = {};
  size_t count_ = 0;
  size_t head_ = 0;
};

struct PolicyInput {
  const char* device_id;          // this device
  const char* active_session_id;  // "" when no session is open
  uint64_t now_ms;                // device uptime
  bool backend_authenticated;     // authenticated, validated-TLS connectivity is up
  const EnrolledTarget* targets;
  size_t target_count;
};

struct PolicyDecision {
  PolicyVerdict verdict;
  bool requires_local_confirmation;  // derived from the operation type
  const EnrolledTarget* target;      // resolved enrolled target, or nullptr
  const char* reason;                // static human-readable reason
};

// Operation classification (single source of truth on the device).
// writes, diagnostic transfer. NOT ble_check_access_policy: its requirement depends on the selected assertion
// (write/transfer, see ADR-005) and is decided when M4 implements it; until then the op is unsupported and runs nothing.
bool operation_requires_local_confirmation(Operation op);
bool operation_requires_target(Operation op);              // every ble_* except ble_discover
bool operation_changes_target_state(Operation op);         // never retried automatically after a timeout

// Per-operation upper bounds on job limits (e.g. scan duration <= 30 s, max_bytes <= 64 KiB).
bool limits_within_bounds(Operation op, const JobLimits& limits);

PolicyDecision evaluate_job(const JobSpec& job, const PolicyInput& in, const ExecutedJobLedger& ledger);

// True only for an approved confirmation whose job id AND nonce match this job, decided no later
// than timeout_ms after the job was received. Server/model booleans are never consulted.
bool confirmation_valid(const JobSpec& job, const LocalConfirmation& c, uint32_t timeout_ms);

}  // namespace oyster
