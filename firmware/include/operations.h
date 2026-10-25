// Operation executors shared by UI-initiated actions and backend jobs (one implementation each).
// Each executor: acquires its radio phase, runs bounded work while polling g_radio.cancel_requested(),
// writes evidence (summary event + pages of <= cfg::kObservationsPerEventPage observations), updates
// g_results, posts AppEvents, releases the phase. Blocking: call only from the worker task.
// Implementation: operations.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "ble_explorer.h"
#include "oyster_core/local_policy.h"
#include "oyster_core/types.h"
#include "wifi_survey.h"

namespace oyster {

struct OpContext {
  const char* session_id;
  const JobSpec* job;              // nullptr for UI-initiated actions
  const EnrolledTarget* target;    // resolved by LocalPolicy; required for connected operations
  const char* room_label;          // wifi_survey only; may be ""
};

struct OpResult {
  EventStatus status;
  JobOutcome outcome;
  char event_ids[cfg::kMaxResultEventIds][cfg::kIdBufLen];
  uint8_t event_count;
  char error[96];
};

OpResult op_wifi_survey(const OpContext& ctx, const WifiSurveyConfig& c);
OpResult op_ble_discover(const OpContext& ctx, const BleDiscoveryConfig& c);
OpResult op_ble_inspect_gatt(const OpContext& ctx);
OpResult op_ble_read_profile_value(const OpContext& ctx, const char* characteristic_id);

// Operations declared in the tool catalogue but not implemented in this firmware build return an
// Unsupported result with this reason (never a placeholder success).
OpResult op_unsupported(const OpContext& ctx, Operation op, const char* reason);

// Typed, bounded argument parsing from JobSpec::args_json (ArduinoJson). False -> job fails as malformed.
bool parse_wifi_survey_args(const JobSpec& j, WifiSurveyConfig& out, char* err, size_t err_len);
bool parse_ble_discover_args(const JobSpec& j, BleDiscoveryConfig& out, char* err, size_t err_len);
bool parse_ble_read_args(const JobSpec& j, char* characteristic_id, size_t id_len, char* err, size_t err_len);

}  // namespace oyster
