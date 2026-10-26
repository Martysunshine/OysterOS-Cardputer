// AssessmentRunner: the single worker task. It executes UI requests and backend jobs one at a time,
// so the UI loop never blocks. Loop: drain UI requests -> (when uplink allowed) upload evidence,
// poll a job, validate with LocalPolicy, obtain local confirmation when the operation type needs it,
// execute via operations.h, submit the result -> refresh the active AI run -> periodic telemetry.
// Jobs for operations not implemented in this build return Unsupported with a reason (M4 ops).
// Implementation: assessment_runner.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/local_policy.h"
#include "oyster_core/types.h"

namespace oyster {

enum class RequestKind : uint8_t {
  WifiSurvey, BleDiscover, InspectGatt, ReadValue, AskOyster, CancelRun, SyncNow, Module,
  VoiceQuestion,  // record on the microphone and upload for transcription (voice_capture.h)
};

struct WorkRequest {
  RequestKind kind;
  char room_label[cfg::kMaxRoomLabelLen + 1];
  char target_id[cfg::kIdBufLen];
  char characteristic_id[24];
  char question[cfg::kMaxQuestionLen + 1];
  bool active_scan;
  uint32_t duration_ms;   // 0 = default
  uint8_t module_id;      // RequestKind::Module: modules::Id
  uint8_t call_id;        //   module-defined call number
  char arg[48];           //   module-defined argument text
};

struct PendingConfirmation {
  bool active;
  char job_id[cfg::kIdBufLen];
  char nonce[cfg::kIdBufLen];
  char operation[32];
  char target_id[cfg::kIdBufLen];
  char effect[96];        // expected effect, shown before execution
  uint32_t remaining_ms;
};

class AssessmentRunner {
 public:
  bool begin();                              // creates the worker task and request queue (depth 4)
  bool enqueue(const WorkRequest& r);        // from the UI; false when the queue is full
  bool busy() const;
  Operation current_op() const;
  uint32_t busy_elapsed_ms() const;
  void cancel_current();                     // -> g_radio.request_cancel()

  bool pending_confirmation(PendingConfirmation& out) const;
  // From the UI confirm screen. Validated with confirmation_valid(): exact job id + nonce, within timeout.
  void resolve_confirmation(const LocalConfirmation& c);

  void set_session(const char* session_id);
  const char* session() const;
  void reload_targets();                     // after provisioning changes
};

extern AssessmentRunner g_runner;

}  // namespace oyster
