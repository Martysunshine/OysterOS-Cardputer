// WifiSurvey: bounded 2.4 GHz AP survey (built-in radio; no 5/6 GHz). Records the scan config used.
// Missing SSIDs, hidden networks and malformed names are normal. WPS/PMF are not obtained here and
// are recorded as "unknown". Implementation: wifi_survey.cpp. Caller holds RadioPhase::WifiSurvey.
#pragma once

#include "oyster_core/types.h"

namespace oyster {

struct WifiSurveyConfig {
  bool passive = true;
  bool show_hidden = true;
  uint32_t ms_per_channel = cfg::kWifiScanMsPerChannel;
  uint8_t channel = 0;      // 0 = all channels
  uint8_t max_results = 0;  // job limit: keep at most this many APs (driver order); 0 = no limit
  uint32_t max_duration_ms = 0;  // job limit: hard stop for the scan; 0 = device default guard
};

struct WifiSurveyResult {
  ApObservation aps[cfg::kMaxApObservations];
  uint8_t count;
  uint16_t driver_count;     // APs the driver reported
  uint16_t overflow;         // driver_count - count: reported but not stored (buffer or max_results)
  bool complete;             // scan finished and every reported AP is stored
  bool cancelled;
  bool passive;
  bool show_hidden;
  uint32_t ms_per_channel;
  uint8_t channel;
  uint32_t started_ms;
  uint32_t duration_ms;
  // Country code advertised by the first stored neighbour AP that carries one. This is an observation
  // of other people's APs, NOT this device's configured regulatory domain. Empty when none advertised.
  char observed_ap_country[3];
  bool limit_hit;            // max_results shaped the stored set
  bool timed_out;            // the scan did not finish within its time guard / max_duration_ms
};

class WifiSurvey {
 public:
  bool start(const WifiSurveyConfig& c);
  PollState poll(WifiSurveyResult& out);  // non-blocking; frees driver buffers on terminal state
  void cancel();

  static void sort_by_rssi(WifiSurveyResult& r);
  static void sort_by_channel(WifiSurveyResult& r);
};

extern WifiSurvey g_wifi_survey;

}  // namespace oyster
