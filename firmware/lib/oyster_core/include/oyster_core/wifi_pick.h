// Pure selection logic for the on-device Wi-Fi setup module (src/modules/wifi_setup.cpp): turn the raw
// AP observations from a scan (oyster_core/types.h ApObservation) into a bounded, deduplicated,
// RSSI-sorted list the user can pick from, and render display-safe SSID text. No Arduino headers; the
// scan itself (radio, WiFi.scanNetworks) lives in the module. Host-tested in test/test_wifisetup.
//
// Raw SSID bytes are not assumed to be printable UTF-8: they are used verbatim to join, and sanitised
// only for display. Hidden/empty SSIDs cannot be joined from a list and are dropped here.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster::wifi_pick {

constexpr size_t kMaxPicks = 24;         // the scan list is bounded to this many networks
constexpr size_t kSsidBytes = 32;        // IEEE 802.11 SSID maximum
constexpr size_t kSsidBufLen = kSsidBytes + 1;

struct Pick {
  uint8_t ssid[kSsidBufLen];  // raw bytes, NUL-terminated; used verbatim to join
  uint8_t ssid_len;           // 1..32 (hidden networks are never stored)
  int8_t rssi_dbm;            // strongest sighting of this SSID
  WifiAuth auth;              // auth of the strongest sighting
  bool open;                  // true: no passphrase (auth == Open)
};

struct PickList {
  Pick items[kMaxPicks];
  uint8_t count;            // networks stored (<= kMaxPicks)
  uint16_t skipped_hidden;  // observations with an empty/hidden SSID (cannot be joined from a list)
  uint16_t merged;          // observations folded into an existing SSID (same name, other BSSID/channel)
  bool overflow;            // more than kMaxPicks distinct SSIDs were seen; the weakest were dropped
};

// Build a bounded, deduplicated, RSSI-sorted pick list from raw observations. Hidden/empty SSIDs are
// skipped; duplicate SSIDs are merged keeping the strongest RSSI (and that sighting's auth). When more
// than kMaxPicks distinct SSIDs appear, the strongest kMaxPicks are kept and `overflow` is set. The
// result is sorted by RSSI descending; equal RSSI keeps first-seen order. Deterministic, no allocation.
void build(const ApObservation* aps, size_t n, PickList& out);

// True when two raw SSIDs (bytes + length) are identical.
bool same_ssid(const uint8_t* a, uint8_t a_len, const uint8_t* b, uint8_t b_len);

// Copy a display-safe SSID into `out` (NUL-terminated, truncated to n-1). Bytes outside printable ASCII
// (0x20..0x7E) become '?'. An empty SSID renders as "(hidden)".
void display_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n);

// Render one scan-list row into `out` (NUL-terminated): the display SSID, with " (open)" appended for
// open networks, with the whole row truncated to at most `cols` visible characters (and to n-1).
void format_row(const Pick& p, char* out, size_t n, size_t cols);

}  // namespace oyster::wifi_pick
