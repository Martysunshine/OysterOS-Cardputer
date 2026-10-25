// ResultsCache: latest radio results for the UI. Written by operations.cpp (worker task), read by the
// UI (loop task) in paged copies. Mutex-protected; all state is file-local.
#include "results_cache.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstring>

namespace oyster {

ResultsCache g_results;

namespace {

SemaphoreHandle_t s_mtx = nullptr;

WifiSurveyResult s_wifi;
char s_wifi_room[cfg::kMaxRoomLabelLen + 1];
WifiSort s_wifi_sort = WifiSort::Rssi;
bool s_has_wifi = false;

BleDiscoveryResult s_ble;
bool s_has_ble = false;

// GATT rows live in BleExplorer's table (one copy, read under its lock); the cache keeps only the metadata.
char s_gatt_target[cfg::kIdBufLen];
bool s_gatt_complete = false;
bool s_gatt_more = false;

ReadResultView s_read;
bool s_has_read = false;

uint32_t s_version[4] = {0, 0, 0, 0};

inline void lock() {
  if (s_mtx != nullptr) xSemaphoreTake(s_mtx, portMAX_DELAY);
}
inline void unlock() {
  if (s_mtx != nullptr) xSemaphoreGive(s_mtx);
}

void apply_wifi_sort() {
  if (s_wifi_sort == WifiSort::Channel) {
    WifiSurvey::sort_by_channel(s_wifi);
  } else {
    WifiSurvey::sort_by_rssi(s_wifi);
  }
}

}  // namespace

void ResultsCache::begin() {
  if (s_mtx == nullptr) s_mtx = xSemaphoreCreateMutex();
  lock();
  std::memset(&s_wifi, 0, sizeof(s_wifi));
  std::memset(&s_ble, 0, sizeof(s_ble));
  std::memset(&s_read, 0, sizeof(s_read));
  s_wifi_room[0] = '\0';
  s_gatt_target[0] = '\0';
  s_gatt_complete = false;
  s_gatt_more = false;
  s_has_wifi = s_has_ble = s_has_read = false;
  s_wifi_sort = WifiSort::Rssi;
  for (auto& v : s_version) v = 0;
  unlock();
}

void ResultsCache::put_wifi(const WifiSurveyResult& r, const char* room_label) {
  lock();
  s_wifi = r;
  std::memset(s_wifi_room, 0, sizeof(s_wifi_room));
  if (room_label != nullptr) std::strncpy(s_wifi_room, room_label, cfg::kMaxRoomLabelLen);
  apply_wifi_sort();
  s_has_wifi = true;
  ++s_version[static_cast<uint8_t>(ResultKind::Wifi)];
  unlock();
}

void ResultsCache::put_ble(const BleDiscoveryResult& r) {
  lock();
  s_ble = r;
  s_has_ble = true;
  ++s_version[static_cast<uint8_t>(ResultKind::Ble)];
  unlock();
}

void ResultsCache::put_ble_from_explorer(const BleDiscoverySummary& sum) {
  lock();  // lock order: cache, then the explorer's buffer lock (the explorer never calls back here)
  std::memset(&s_ble, 0, sizeof(s_ble));
  const size_t got = g_ble.discovery_rows(0, s_ble.adv, sum.count);
  s_ble.count = static_cast<uint8_t>(got);
  s_ble.overflow = sum.overflow;
  s_ble.malformed = sum.malformed;
  s_ble.callbacks = sum.callbacks;
  s_ble.dropped_in_callback = sum.dropped_in_callback;
  s_ble.complete = sum.complete && got == sum.count;
  s_ble.cancelled = sum.cancelled;
  s_ble.active_scan = sum.active_scan;
  s_ble.started_ms = sum.started_ms;
  s_ble.duration_ms = sum.duration_ms;
  s_ble.interval_ms = sum.interval_ms;
  s_ble.window_ms = sum.window_ms;
  s_ble.limit_hit = sum.limit_hit;
  s_has_ble = true;
  ++s_version[static_cast<uint8_t>(ResultKind::Ble)];
  unlock();
}

void ResultsCache::put_gatt(const char* target_id, const GattPage& p) {
  lock();
  if (p.page_index == 0) {
    std::memset(s_gatt_target, 0, sizeof(s_gatt_target));
    if (target_id != nullptr) std::strncpy(s_gatt_target, target_id, cfg::kIdBufLen - 1);
  }
  s_gatt_complete = p.complete && !p.more_pages;
  s_gatt_more = p.more_pages;
  ++s_version[static_cast<uint8_t>(ResultKind::Gatt)];
  unlock();
}

void ResultsCache::put_read(const ReadResultView& r) {
  lock();
  s_read = r;
  s_has_read = true;
  ++s_version[static_cast<uint8_t>(ResultKind::Read)];
  unlock();
}

void ResultsCache::set_wifi_sort(WifiSort s) {
  lock();
  s_wifi_sort = s;
  if (s_has_wifi) apply_wifi_sort();
  ++s_version[static_cast<uint8_t>(ResultKind::Wifi)];
  unlock();
}

uint32_t ResultsCache::version(ResultKind k) const {
  lock();
  uint32_t v = s_version[static_cast<uint8_t>(k)];
  unlock();
  return v;
}

size_t ResultsCache::wifi_rows(size_t start, ApObservation* out, size_t n, WifiSummary& summary) const {
  lock();
  summary.count = s_wifi.count;
  summary.overflow = s_wifi.overflow;
  summary.complete = s_wifi.complete;
  summary.cancelled = s_wifi.cancelled;
  summary.passive = s_wifi.passive;
  summary.duration_ms = s_wifi.duration_ms;
  std::memset(summary.room_label, 0, sizeof(summary.room_label));
  std::strncpy(summary.room_label, s_wifi_room, cfg::kMaxRoomLabelLen);
  size_t copied = 0;
  for (size_t i = start; i < s_wifi.count && copied < n; ++i) out[copied++] = s_wifi.aps[i];
  unlock();
  return copied;
}

size_t ResultsCache::ble_rows(size_t start, BleAdvObservation* out, size_t n, BleSummary& summary) const {
  lock();
  summary.count = s_ble.count;
  summary.overflow = s_ble.overflow;
  summary.malformed = s_ble.malformed;
  summary.complete = s_ble.complete;
  summary.cancelled = s_ble.cancelled;
  summary.active_scan = s_ble.active_scan;
  summary.duration_ms = s_ble.duration_ms;
  size_t copied = 0;
  for (size_t i = start; i < s_ble.count && copied < n; ++i) out[copied++] = s_ble.adv[i];
  unlock();
  return copied;
}

size_t ResultsCache::gatt_rows(size_t start, GattEntry* out, size_t n, GattSummary& summary) const {
  lock();
  std::memset(summary.target_id, 0, sizeof(summary.target_id));
  std::strncpy(summary.target_id, s_gatt_target, cfg::kIdBufLen - 1);
  uint16_t total = 0;
  const size_t copied = g_ble.gatt_rows(start, out, n, total);  // lock order: cache, then explorer
  summary.count = total;
  summary.complete = s_gatt_complete;
  summary.more_pages = s_gatt_more;
  unlock();
  return copied;
}

bool ResultsCache::latest_read(ReadResultView& out) const {
  lock();
  bool has = s_has_read;
  if (has) out = s_read;
  unlock();
  return has;
}

bool ResultsCache::ble_by_obs_id(uint16_t obs_id, BleAdvObservation& out) const {
  lock();
  bool found = false;
  for (uint8_t i = 0; i < s_ble.count; ++i) {
    if (s_ble.adv[i].obs_id == obs_id) {
      out = s_ble.adv[i];
      found = true;
      break;
    }
  }
  unlock();
  return found;
}

}  // namespace oyster
