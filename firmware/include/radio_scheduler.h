// RadioScheduler: single owner of the shared 2.4 GHz path (Wi-Fi and BLE share RF).
// One active phase: Idle -> WifiSurvey | BleDiscovery -> BleSession -> UplinkSync -> Idle.
// Discovery pauses during a connected session; backend polling runs only in Idle/UplinkSync.
// Cancellation is cooperative. Implementation: radio_scheduler.cpp. Thread-safe.
#pragma once

#include <cstdint>

namespace oyster {

enum class RadioPhase : uint8_t { Idle, WifiSurvey, BleDiscovery, BleSession, UplinkSync };
const char* to_string(RadioPhase p);

class RadioScheduler {
 public:
  void begin();
  bool acquire(RadioPhase phase, uint32_t wait_ms = 0);  // false if another phase is active
  void release(RadioPhase phase);                        // returns to Idle if phase is active
  RadioPhase active() const;
  uint32_t active_since_ms() const;

  void request_cancel();          // UI emergency cancel / backend cancel
  bool cancel_requested() const;  // polled by every bounded loop
  void clear_cancel();            // by the phase owner once stopped

  bool uplink_allowed() const;    // true in Idle or UplinkSync
};

extern RadioScheduler g_radio;

}  // namespace oyster
