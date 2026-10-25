// Latest results for the UI. Writers: operations.cpp. Reader: the UI, which copies only the rows it
// draws (paged copies) so it never holds a second full result in RAM. Implementation: results_cache.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "app_events.h"
#include "ble_explorer.h"
#include "oyster_core/types.h"
#include "wifi_survey.h"

namespace oyster {

struct WifiSummary {
  uint8_t count;
  uint16_t overflow;
  bool complete;
  bool cancelled;
  bool passive;
  uint32_t duration_ms;
  char room_label[cfg::kMaxRoomLabelLen + 1];
};

struct BleSummary {
  uint8_t count;
  uint16_t overflow;
  uint16_t malformed;
  bool complete;
  bool cancelled;
  bool active_scan;
  uint32_t duration_ms;
};

struct GattSummary {
  char target_id[cfg::kIdBufLen];
  uint16_t count;
  bool complete;
  bool more_pages;
};

struct ReadResultView {
  char target_id[cfg::kIdBufLen];
  char characteristic_id[24];
  EventStatus status;
  uint16_t value_length;
  uint8_t preview[16];     // only for characteristics whose profile read policy is public
  uint8_t preview_len;
  bool preview_withheld;   // value not shown (non-public policy): length/hash only
  SecurityState sec;
  char event_id[cfg::kIdBufLen];
};

enum class WifiSort : uint8_t { Rssi, Channel };

class ResultsCache {
 public:
  void begin();
  void put_wifi(const WifiSurveyResult& r, const char* room_label);
  void put_ble(const BleDiscoveryResult& r);
  // Stores the explorer's frozen working set (rows copied straight from BleExplorer under its lock), so
  // the caller needs no full-size BleDiscoveryResult. Call after poll_discovery_summary() went terminal.
  void put_ble_from_explorer(const BleDiscoverySummary& sum);
  void put_gatt(const char* target_id, const GattPage& p);
  void put_read(const ReadResultView& r);
  void set_wifi_sort(WifiSort s);

  uint32_t version(ResultKind k) const;
  // Paged copies: return the number of rows copied starting at `start`.
  size_t wifi_rows(size_t start, ApObservation* out, size_t n, WifiSummary& summary) const;
  size_t ble_rows(size_t start, BleAdvObservation* out, size_t n, BleSummary& summary) const;
  size_t gatt_rows(size_t start, GattEntry* out, size_t n, GattSummary& summary) const;
  bool latest_read(ReadResultView& out) const;
  bool ble_by_obs_id(uint16_t obs_id, BleAdvObservation& out) const;
};

extern ResultsCache g_results;

}  // namespace oyster
