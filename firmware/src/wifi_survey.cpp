// WifiSurvey: bounded 2.4 GHz AP survey via the Arduino WiFi scan API (ESP-IDF underneath).
// Non-blocking: start an async scan, poll WiFi.scanComplete(), always scanDelete() on any terminal
// state. Raw SSID bytes come from the driver AP record (not assumed UTF-8).
#include "wifi_survey.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include <cstring>

namespace oyster {

WifiSurvey g_wifi_survey;

namespace {
bool s_active = false;
bool s_cancelled = false;
uint32_t s_started_ms = 0;
WifiSurveyConfig s_cfg;

// Safety ceiling so a wedged driver scan cannot run forever.
constexpr uint32_t kScanGuardMs = cfg::kWifiScanMaxMs + 5000;
constexpr uint32_t kLimitSlackMs = 1500;  // driver start-up / result delivery on top of max_duration_ms

WifiAuth map_auth(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN:            return WifiAuth::Open;
    case WIFI_AUTH_WEP:             return WifiAuth::Wep;
    case WIFI_AUTH_WPA_PSK:         return WifiAuth::WpaPsk;
    case WIFI_AUTH_WPA2_PSK:        return WifiAuth::Wpa2Psk;
    case WIFI_AUTH_WPA_WPA2_PSK:    return WifiAuth::WpaWpa2Psk;
    case WIFI_AUTH_WPA2_ENTERPRISE: return WifiAuth::Wpa2Enterprise;
    case WIFI_AUTH_WPA3_PSK:        return WifiAuth::Wpa3Psk;
    case WIFI_AUTH_WPA2_WPA3_PSK:   return WifiAuth::Wpa2Wpa3Psk;
    case WIFI_AUTH_WAPI_PSK:        return WifiAuth::WapiPsk;
    default:                        return WifiAuth::Unknown;
  }
}

void fill_config_echo(WifiSurveyResult& out) {
  out.passive = s_cfg.passive;
  out.show_hidden = s_cfg.show_hidden;
  out.ms_per_channel = s_cfg.ms_per_channel;
  out.channel = s_cfg.channel;
  out.started_ms = s_started_ms;
  out.duration_ms = millis() - s_started_ms;
}

void clear_result(WifiSurveyResult& out) {
  out.count = 0;
  out.driver_count = 0;
  out.overflow = 0;
  out.complete = false;
  out.cancelled = false;
  out.observed_ap_country[0] = '\0';
  out.observed_ap_country[1] = '\0';
  out.observed_ap_country[2] = '\0';
  out.limit_hit = false;
  out.timed_out = false;
}

// Copy up to min(kMaxApObservations, max_results) records, in driver order, into the result. Records the
// driver reported but that are not stored (buffer full, job limit, or no record available) are counted
// in overflow, so the result never looks complete.
void collect(int driver_count, uint8_t max_results, WifiSurveyResult& out) {
  out.driver_count = (driver_count > 0xFFFF) ? 0xFFFF : static_cast<uint16_t>(driver_count);
  int cap = static_cast<int>(cfg::kMaxApObservations);
  if (max_results != 0 && max_results < cap) cap = max_results;
  int want = driver_count < 0 ? 0 : driver_count;
  if (want > cap) want = cap;
  out.limit_hit = (max_results != 0 && driver_count > static_cast<int>(max_results));
  out.observed_ap_country[0] = '\0';
  int stored = 0;
  for (int i = 0; i < want; ++i) {
    const wifi_ap_record_t* rec =
        reinterpret_cast<const wifi_ap_record_t*>(WiFi.getScanInfoByIndex(i));
    if (rec == nullptr) continue;  // never invent an observation the driver did not give us
    ApObservation& ap = out.aps[stored++];
    std::memset(&ap, 0, sizeof(ap));
    // Raw SSID bytes (driver stores a NUL-terminated copy, max 32).
    size_t slen = strnlen(reinterpret_cast<const char*>(rec->ssid), cfg::kMaxSsidBytes);
    ap.ssid_len = static_cast<uint8_t>(slen);
    std::memcpy(ap.ssid, rec->ssid, slen);
    std::memcpy(ap.bssid, rec->bssid, 6);
    ap.channel = rec->primary;
    ap.rssi_dbm = rec->rssi;
    ap.auth = map_auth(rec->authmode);
    // Country advertised by the first stored neighbour AP that carries one (not our own domain).
    if (out.observed_ap_country[0] == '\0' && rec->country.cc[0] >= 'A' && rec->country.cc[0] <= 'Z') {
      out.observed_ap_country[0] = rec->country.cc[0];
      out.observed_ap_country[1] = (rec->country.cc[1] >= 'A' && rec->country.cc[1] <= 'Z') ? rec->country.cc[1] : '\0';
      out.observed_ap_country[2] = '\0';
    }
  }
  out.count = static_cast<uint8_t>(stored);
  const int total = driver_count < 0 ? 0 : driver_count;
  out.overflow = (total > stored) ? static_cast<uint16_t>(total - stored) : 0;
}
}  // namespace

bool WifiSurvey::start(const WifiSurveyConfig& c) {
  if (s_active) return false;
  s_cfg = c;
  s_cancelled = false;
  s_started_ms = millis();
  // async = true -> returns immediately; poll() drives completion.
  int16_t rc = WiFi.scanNetworks(/*async=*/true, c.show_hidden, c.passive, c.ms_per_channel, c.channel);
  if (rc == WIFI_SCAN_FAILED) {
    s_active = false;
    return false;
  }
  s_active = true;
  return true;
}

PollState WifiSurvey::poll(WifiSurveyResult& out) {
  clear_result(out);
  fill_config_echo(out);
  if (!s_active) {
    out.complete = false;
    return PollState::Failed;
  }

  if (s_cancelled) {
    esp_wifi_scan_stop();
    WiFi.scanDelete();
    s_active = false;
    out.cancelled = true;
    out.complete = false;
    out.duration_ms = millis() - s_started_ms;
    return PollState::Cancelled;
  }

  int16_t n = WiFi.scanComplete();
  if (n == WIFI_SCAN_RUNNING) {
    const uint32_t guard = (s_cfg.max_duration_ms != 0) ? s_cfg.max_duration_ms + kLimitSlackMs : kScanGuardMs;
    if (millis() - s_started_ms > guard) {
      esp_wifi_scan_stop();
      WiFi.scanDelete();
      s_active = false;
      out.complete = false;
      out.timed_out = true;
      out.duration_ms = millis() - s_started_ms;
      return PollState::Failed;
    }
    return PollState::Running;
  }
  if (n == WIFI_SCAN_FAILED) {
    WiFi.scanDelete();
    s_active = false;
    out.complete = false;
    out.duration_ms = millis() - s_started_ms;
    return PollState::Failed;
  }

  // n >= 0: scan finished.
  collect(n, s_cfg.max_results, out);
  WiFi.scanDelete();
  s_active = false;
  out.complete = (out.overflow == 0);  // finished, but not complete if any reported AP could not be stored
  out.cancelled = false;
  out.duration_ms = millis() - s_started_ms;
  return PollState::Done;
}

void WifiSurvey::cancel() { s_cancelled = true; }

void WifiSurvey::sort_by_rssi(WifiSurveyResult& r) {
  // Descending RSSI; insertion sort over the small bounded view.
  for (size_t i = 1; i < r.count; ++i) {
    ApObservation key = r.aps[i];
    size_t j = i;
    while (j > 0 && r.aps[j - 1].rssi_dbm < key.rssi_dbm) {
      r.aps[j] = r.aps[j - 1];
      --j;
    }
    r.aps[j] = key;
  }
}

void WifiSurvey::sort_by_channel(WifiSurveyResult& r) {
  for (size_t i = 1; i < r.count; ++i) {
    ApObservation key = r.aps[i];
    size_t j = i;
    while (j > 0 && r.aps[j - 1].channel > key.channel) {
      r.aps[j] = r.aps[j - 1];
      --j;
    }
    r.aps[j] = key;
  }
}

}  // namespace oyster
