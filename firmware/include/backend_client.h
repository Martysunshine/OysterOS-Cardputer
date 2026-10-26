// BackendClient: device-initiated HTTPS to the backend over one persistent validated-TLS connection
// (see docs/API.md). Polls jobs, uploads bounded event batches with sequence ranges, submits idempotent
// results, relays AI run status for the UI. No inbound port. Implementation: backend_client.cpp.
// Runs on its own task; pauses during RadioPhase::BleSession.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster {

enum class BackendState : uint8_t { Offline, Connecting, ClockNotSet, CertError, AuthError, Connected };
const char* to_string(BackendState s);

struct RunSummary {
  char run_id[cfg::kIdBufLen];
  char status[24];
  char answer_excerpt[1200];  // the device shows this much of the answer (scrollable); the report keeps all of it
  bool has_pending_action;
  char pending_operation[32];
  char pending_target[cfg::kIdBufLen];
  char pending_effect[96];
};

class BackendClient {
 public:
  bool begin();
  BackendState state() const;

  // Job delivery. Returns true and fills `out` when a validated job is available for this device.
  bool poll_job(JobSpec& out);
  bool submit_result(const char* job_id, JobOutcome outcome, const char* const* event_ids,
                     size_t n_events, const char* error, const LocalConfirmation* confirmation);

  // Evidence upload. The store provides serialized lines; the client batches within kMaxApiPageBytes.
  bool upload_pending_events();

  // Lifecycle reports between delivery and result: "awaiting_local_confirmation", "running".
  bool report_job_status(const char* job_id, const char* status);

  // AI runs (UI surface for "Ask Oyster").
  bool start_run(const char* session_id, const char* question, char out_run_id[cfg::kIdBufLen]);
  bool poll_run(const char* run_id, RunSummary& out);
  bool cancel_run(const char* run_id);

  // Sessions are generated on the device; the backend registers and binds them (idempotent).
  bool register_session(const char* session_id);
  bool fetch_time(uint64_t& unix_seconds);   // unauthenticated GET /v1/time clock hint
  const RunSummary& latest_run() const;
  // Generic authenticated request for the modules (hosts, deck, home). Returns the HTTP status, or a negative
  // value when the transport failed or the response was too large. The response is copied into `resp`
  // (NUL-terminated, truncated at cap-1). Only /v1/ paths. `idempotent`: one automatic retry on a dead
  // keep-alive connection is allowed; pass false for anything that must not run twice.
  // `idempotency_key` (optional, <= 71 chars) is sent as the Idempotency-Key header.
  int call(const char* method, const char* path, const char* body, size_t body_len, char* resp, size_t cap,
           size_t* resp_len, bool idempotent, const char* idempotency_key = nullptr);
  // Drop the cached token, CA and connection (after FACTORY-RESET or re-provisioning) so the next request
  // re-reads DeviceConfig. Safe to call from any task.
  void invalidate_credentials();
};

extern BackendClient g_backend;

}  // namespace oyster
