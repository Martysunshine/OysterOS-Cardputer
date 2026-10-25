// Wire names for the shared enums. Every string here must match schemas/*.schema.json exactly.
#include "oyster_core/types.h"

#include <cstring>

namespace oyster {

const char* to_string(SourceMode v) {
  switch (v) {
    case SourceMode::Live: return "live";
    case SourceMode::Offline: return "offline";
    case SourceMode::Simulated: return "simulated";
  }
  return "unknown";
}

const char* to_string(Transport v) {
  switch (v) {
    case Transport::Wifi: return "wifi";
    case Transport::Ble: return "ble";
    case Transport::Backend: return "backend";
    case Transport::Local: return "local";
    case Transport::None: return "none";
  }
  return "none";
}

const char* to_string(ClockQuality v) {
  switch (v) {
    case ClockQuality::Ntp: return "ntp";
    case ClockQuality::Serial: return "serial";
    case ClockQuality::BackendHint: return "backend_hint";
    case ClockQuality::Unknown: return "unknown";
  }
  return "unknown";
}

const char* to_string(EventStatus v) {
  switch (v) {
    case EventStatus::Success: return "success";
    case EventStatus::Denied: return "denied";
    case EventStatus::Timeout: return "timeout";
    case EventStatus::Unsupported: return "unsupported";
    case EventStatus::Error: return "error";
    case EventStatus::Cancelled: return "cancelled";
    case EventStatus::Partial: return "partial";
    case EventStatus::UnknownOutcome: return "unknown_outcome";
    case EventStatus::Inconclusive: return "inconclusive";
  }
  return "error";
}

namespace {
// Indexed by Operation; the first kJobOperationCount entries are the job.schema.json enum.
constexpr const char* kOperationNames[] = {
    "wifi_survey", "ble_discover", "ble_inspect_gatt", "ble_read_profile_value",
    "ble_observe_notifications", "ble_check_access_policy", "ble_write_lab_value",
    "ble_collect_diagnostics",
    "boot", "telemetry", "session_open", "session_close", "local_confirmation", "recovery",
    "unknown",
};
constexpr size_t kOperationNameCount = sizeof(kOperationNames) / sizeof(kOperationNames[0]);
static_assert(kOperationNameCount == static_cast<size_t>(Operation::Unknown) + 1,
              "operation name table out of sync with enum");
}  // namespace

const char* to_string(Operation v) {
  const size_t i = static_cast<size_t>(v);
  return i < kOperationNameCount ? kOperationNames[i] : "unknown";
}

const char* to_string(JobOutcome v) {
  switch (v) {
    case JobOutcome::Succeeded: return "succeeded";
    case JobOutcome::Failed: return "failed";
    case JobOutcome::Cancelled: return "cancelled";
    case JobOutcome::Expired: return "expired";
    case JobOutcome::UnknownOutcome: return "unknown_outcome";
    case JobOutcome::Unsupported: return "unsupported";
  }
  return "failed";
}

bool parse_operation(const char* s, Operation& out) {
  if (s == nullptr) return false;
  for (uint8_t i = 0; i < kJobOperationCount; ++i) {
    if (std::strcmp(s, kOperationNames[i]) == 0) {
      out = static_cast<Operation>(i);
      return true;
    }
  }
  return false;
}

bool is_job_operation(Operation op) { return static_cast<uint8_t>(op) < kJobOperationCount; }

const char* to_string(WifiAuth v) {
  switch (v) {
    case WifiAuth::Open: return "OPEN";
    case WifiAuth::Wep: return "WEP";
    case WifiAuth::WpaPsk: return "WPA_PSK";
    case WifiAuth::Wpa2Psk: return "WPA2_PSK";
    case WifiAuth::WpaWpa2Psk: return "WPA_WPA2_PSK";
    case WifiAuth::Wpa2Enterprise: return "WPA2_ENTERPRISE";
    case WifiAuth::Wpa3Psk: return "WPA3_PSK";
    case WifiAuth::Wpa2Wpa3Psk: return "WPA2_WPA3_PSK";
    case WifiAuth::WapiPsk: return "WAPI_PSK";
    case WifiAuth::Unknown: return "UNKNOWN";
  }
  return "UNKNOWN";
}

const char* to_string(BleAddrType v) {
  switch (v) {
    case BleAddrType::Public: return "public";
    case BleAddrType::Random: return "random";
    case BleAddrType::Unknown: return "unknown";
  }
  return "unknown";
}

}  // namespace oyster
