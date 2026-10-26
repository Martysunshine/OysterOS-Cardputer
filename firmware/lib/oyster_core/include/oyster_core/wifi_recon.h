// Pure logic for the on-device Wi-Fi recon / wardriving module (src/modules/wifi_recon.cpp): an
// accumulating, de-duplicated table of access points seen across repeated 2.4 GHz scans, plus the
// display and CSV formatters the module needs. No Arduino headers; the radio scan and the SD writes
// live in the module. Host-tested in test/test_wifirecon.
//
// Scope: this is a passive listener. The table records what was observed (BSSID, SSID bytes, channel,
// auth as reported, RSSI). Raw SSID bytes are not assumed to be printable UTF-8: kept verbatim, and
// sanitised only for display and for CSV (non-printable -> '?'). A hidden network has ssid_len == 0.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster::wifi_recon {

// The accumulated table is bounded well above one scan (kMaxApObservations == 32) so several scans of a
// busy area fit. 128 entries ~= 128 * ~56 B ~= 7 KiB: kept static by the module, never on the stack.
constexpr size_t kMaxApEntries = 128;
constexpr size_t kSsidBytes = cfg::kMaxSsidBytes;  // 32
constexpr size_t kBssidStrLen = 18;                // "aa:bb:cc:dd:ee:ff" + NUL

// One unique access point, keyed by BSSID, accumulated across scans.
struct ApEntry {
  uint8_t bssid[6];
  uint8_t ssid[kSsidBytes];  // raw bytes (not NUL-terminated); ssid_len bytes are valid
  uint8_t ssid_len;          // 0 = hidden / empty SSID
  uint8_t channel;           // last channel seen on
  WifiAuth auth;             // last auth reported
  int8_t max_rssi;           // strongest sighting
  int8_t last_rssi;          // most recent sighting
  uint32_t first_seen_ms;    // device clock (millis) of the first sighting
  uint32_t last_seen_ms;     // device clock (millis) of the most recent sighting
  uint16_t times_seen;       // number of sightings merged into this entry
};

// What merge() did with one observation.
enum class Merge : uint8_t {
  Added,    // a new BSSID was stored
  Updated,  // an existing BSSID was refreshed
  Dropped,  // the table was full and this observation was weaker than every stored entry
};

// Accumulating AP table, de-duplicated by BSSID. Pure data, no allocation; copy/move safe.
class ApTable {
 public:
  void clear();

  // Merge one observation sighted at now_ms. Dedupe by BSSID: an existing entry's last_rssi/last_seen/
  // channel/auth are refreshed, max_rssi grows, times_seen increments, and a previously hidden SSID is
  // filled in once a named sighting arrives. A new BSSID is appended while there is room; when the table
  // is full the weakest-RSSI entry (oldest first_seen breaks ties) is evicted for a stronger newcomer,
  // otherwise the newcomer is dropped. Returns what happened.
  Merge merge(const ApObservation& ap, uint32_t now_ms);

  // Sort entries by max_rssi descending; stable (equal RSSI keeps earlier position).
  void sort_by_rssi();

  size_t count() const { return count_; }
  const ApEntry& at(size_t i) const { return entries_[i]; }
  // Index of the entry with this BSSID, or -1. Linear over a small bounded table.
  int find(const uint8_t bssid[6]) const;
  // APs discarded because the table was full: an evicted weak entry, or a dropped weak newcomer.
  uint32_t dropped() const { return dropped_; }
  static constexpr size_t capacity() { return kMaxApEntries; }

 private:
  size_t weakest() const;  // index of the weakest entry (caller guarantees count_ > 0)

  ApEntry entries_[kMaxApEntries] = {};
  size_t count_ = 0;
  uint32_t dropped_ = 0;
};

// Short auth label for a list row / CSV: "OPEN","WEP","WPA","WPA2","WPA2E","WPA3","WAPI","?"
// (mixed modes collapse to their strongest member). At most 5 characters.
const char* auth_short(WifiAuth v);

// Write "aa:bb:cc:dd:ee:ff" (lowercase) into out (NUL-terminated; needs n >= kBssidStrLen).
void bssid_to_str(const uint8_t bssid[6], char* out, size_t n);

// Display-safe SSID into out (NUL-terminated, truncated to n-1). Bytes outside printable ASCII
// (0x20..0x7E) become '?'. An empty SSID renders as "<hidden>".
void display_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n);

// One main-page row into out (NUL-terminated): "<rssi> ch<ch> <auth> <ssid>", e.g. "-54 ch6 WPA2 Net",
// clipped to at most `cols` visible characters (and to n-1). RSSI is the strongest sighting (max_rssi),
// matching the RSSI-descending sort.
void format_row(const ApEntry& e, char* out, size_t n, size_t cols);

// Relative age into out (NUL-terminated): "now", "<n>s ago", "<n>m ago", "<n>h ago", "<n>d ago".
void format_relative(uint32_t delta_ms, char* out, size_t n);

// CSV header line (no trailing newline): the column order format_csv_line writes.
const char* csv_header();

// One CSV record for a single sighting (no trailing newline) into out (NUL-terminated, truncated to
// n-1). Columns: ts, bssid, channel, rssi, auth, hidden, ssid. `ts` is the device clock value the caller
// logs (real epoch, or millis-since-boot when no clock is set: the file header records which). The SSID
// field is CSV-escaped: non-printable bytes become '?', and a field containing a comma or quote is
// wrapped in double quotes with internal quotes doubled. A hidden SSID writes an empty ssid field and
// hidden=1. Recommend n >= 96.
void format_csv_line(const ApObservation& ap, uint32_t ts, char* out, size_t n);

// Escape one SSID as a CSV field (used by format_csv_line; exposed for testing). Non-printable bytes
// become '?'; the field is quoted only when it contains a comma or a double quote. Empty SSID -> "".
void csv_escape_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n);

}  // namespace oyster::wifi_recon
