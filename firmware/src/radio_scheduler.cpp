// RadioScheduler: single owner of the shared 2.4 GHz path. One phase at a time, cooperative cancel.
#include "radio_scheduler.h"

#include <Arduino.h>

#include <atomic>

namespace oyster {

RadioScheduler g_radio;

const char* to_string(RadioPhase p) {
  switch (p) {
    case RadioPhase::Idle:         return "idle";
    case RadioPhase::WifiSurvey:   return "wifi_survey";
    case RadioPhase::BleDiscovery: return "ble_discovery";
    case RadioPhase::BleSession:   return "ble_session";
    case RadioPhase::UplinkSync:   return "uplink_sync";
  }
  return "idle";
}

namespace {
std::atomic<uint8_t> s_phase{static_cast<uint8_t>(RadioPhase::Idle)};
std::atomic<uint32_t> s_since_ms{0};
std::atomic<bool> s_cancel{false};
}  // namespace

void RadioScheduler::begin() {
  s_phase.store(static_cast<uint8_t>(RadioPhase::Idle));
  s_since_ms.store(0);
  s_cancel.store(false);
}

bool RadioScheduler::acquire(RadioPhase phase, uint32_t wait_ms) {
  const uint8_t want = static_cast<uint8_t>(phase);
  const uint32_t deadline = millis() + wait_ms;
  for (;;) {
    uint8_t expected = static_cast<uint8_t>(RadioPhase::Idle);
    if (s_phase.compare_exchange_strong(expected, want)) {
      s_since_ms.store(millis());
      return true;
    }
    if (expected == want) return true;  // already held by this phase
    if (wait_ms == 0) return false;
    if (static_cast<int32_t>(millis() - deadline) >= 0) return false;
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void RadioScheduler::release(RadioPhase phase) {
  uint8_t expected = static_cast<uint8_t>(phase);
  // Only the owning phase returns the path to Idle.
  s_phase.compare_exchange_strong(expected, static_cast<uint8_t>(RadioPhase::Idle));
}

RadioPhase RadioScheduler::active() const {
  return static_cast<RadioPhase>(s_phase.load());
}

uint32_t RadioScheduler::active_since_ms() const {
  return s_since_ms.load();
}

void RadioScheduler::request_cancel() { s_cancel.store(true); }
bool RadioScheduler::cancel_requested() const { return s_cancel.load(); }
void RadioScheduler::clear_cancel() { s_cancel.store(false); }

bool RadioScheduler::uplink_allowed() const {
  const RadioPhase p = active();
  return p == RadioPhase::Idle || p == RadioPhase::UplinkSync;
}

}  // namespace oyster
