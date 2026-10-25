// BleExplorer: NimBLE-Arduino 2.x observer/central. Discovery never connects. Connections only to an
// enrolled target resolved by LocalPolicy, one at a time. Values the stack does not expose are
// recorded as unknown/null. Implementation: ble_explorer.cpp.
// Discovery requires RadioPhase::BleDiscovery; connected work requires RadioPhase::BleSession.
//
// Identity fails closed: connect() verifies the profile's public device_info value (read by UUID, no
// service enumeration, no pairing) before it returns Success. discover_gatt() and read_by_uuid()
// refuse to run on a link whose identity is not verified. Pairing/bonding is enabled only after that.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/local_policy.h"
#include "oyster_core/types.h"

namespace oyster {

struct BleDiscoveryConfig {
  uint32_t duration_ms = cfg::kBleDiscoveryMs;
  bool active = false;
  uint16_t interval_ms = 100;
  uint16_t window_ms = 100;
  uint8_t max_results = 0;  // job limit: stop collecting beyond this many devices; 0 = no limit
};

struct BleDiscoveryResult {
  BleAdvObservation adv[cfg::kMaxBleAdverts];
  uint8_t count;
  uint16_t overflow;
  uint16_t malformed;
  uint32_t callbacks;
  uint32_t dropped_in_callback;
  bool complete;
  bool cancelled;
  bool active_scan;
  uint32_t started_ms;
  uint32_t duration_ms;
  uint16_t interval_ms;
  uint16_t window_ms;
  bool limit_hit;           // collection stopped because BleDiscoveryConfig::max_results was reached
};

// Everything in BleDiscoveryResult except the rows. Rows stay in BleExplorer's working set and are
// copied out in small pages with discovery_rows(), so no operation needs a full-size copy on its stack.
struct BleDiscoverySummary {
  uint8_t count;
  uint16_t overflow;
  uint16_t malformed;
  uint32_t callbacks;
  uint32_t dropped_in_callback;
  bool complete;            // ran to its scheduled end and nothing was dropped, overflowed or limited
  bool cancelled;
  bool active_scan;
  bool limit_hit;
  uint32_t started_ms;
  uint32_t duration_ms;
  uint16_t interval_ms;
  uint16_t window_ms;
};

struct BleOpStatus {
  EventStatus status;
  int nimble_rc;           // 0 when not reported
  int disconnect_reason;   // -1 when none
  bool att_status_known;
  uint8_t att_status;
  SecurityState before;
  SecurityState after;
  bool state_changed;      // before != after
  char detail[64];
};

struct ConnectedTargetInfo {
  char target_id[cfg::kIdBufLen];
  uint8_t addr[6];
  BleAddrType addr_type;
  uint16_t mtu;
  TriBool identity_ok;     // true only when the public device_info matched exactly; unknown when not decided
  char identity_note[48];
  uint8_t identity_len;    // length of the device_info value as served (0 when none was returned)
  uint8_t identity_value[16];  // first bytes of that public value, kept as evidence of a mismatch
  int identity_status;     // host status of the identity read: 0 ok, -1 not attempted
};

// Limits applied to the next connected session; reset with an empty BleOpLimits.
struct BleOpLimits {
  uint32_t max_duration_ms = 0;  // from set_limits(); connect/enumeration/read stop with Timeout; 0 = none
  uint16_t max_entries = 0;      // GATT entries kept by discover_gatt(); 0 = none
};

class BleExplorer {
 public:
  bool begin();
  bool start_discovery(const BleDiscoveryConfig& c);
  // Non-blocking. Always fills the running counts; sets complete/cancelled at a terminal state, after
  // which late scan callbacks are ignored so the rows stay stable for discovery_rows().
  PollState poll_discovery_summary(BleDiscoverySummary& out);
  // Copies up to n rows starting at `start` from the working set (under its lock).
  size_t discovery_rows(size_t start, BleAdvObservation* out, size_t n) const;
  // Whole-result form: needs a ~9.5 KB destination; prefer the two calls above.
  PollState poll_discovery(BleDiscoveryResult& out);
  void stop_discovery();

  void set_limits(const BleOpLimits& l);

  // Connects to t.address (+type) only. Returns Success after the identity check passed. Other results
  // (the link is already closed): Denied = device_info mismatch, Inconclusive = device_info unreadable,
  // Timeout, Cancelled, Error.
  BleOpStatus connect(const EnrolledTarget& t, uint32_t timeout_ms);
  BleOpStatus discover_gatt(uint16_t page_index, GattPage& out);
  // full_len (optional) receives the length of the value as served, even when more than `max` bytes.
  BleOpStatus read_by_uuid(const uint8_t service_uuid[16], const uint8_t char_uuid[16],
                           uint8_t* out, size_t max, size_t& out_len, size_t* full_len = nullptr);
  // Rows of the last GATT enumeration (absolute parent indices), copied under a lock; they stay valid after
  // disconnect(). The table is the only copy: ResultsCache reads it through this call. `total` receives the row count.
  size_t gatt_rows(size_t start, GattEntry* out, size_t n, uint16_t& total) const;
  SecurityState security_state() const;
  const ConnectedTargetInfo& connected_info() const;
  bool connected() const;
  void disconnect();

  // Safe from any task. Requests cancel for the running operation (also raises the radio-wide cancel
  // flag). Interrupts a pending connect (cancelConnect) and, while a blocking GATT step is in flight,
  // tears the link down so that step returns. A single in-flight ATT request otherwise cannot be
  // interrupted; the next step boundary honours the request.
  void cancel();
};

extern BleExplorer g_ble;

}  // namespace oyster
