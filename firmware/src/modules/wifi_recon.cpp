// Wi-Fi recon / wardriving module (Home tile 3 "WiFi Scan"): a standalone, on-device tool that repeatedly
// scans the 2.4 GHz band, accumulates a de-duplicated table of the access points around you, shows them
// live, and logs each sighting to the SD card. No backend, no session, no network: pure radio + SD, so it
// works with the laptop off. This tool only listens; it never connects, deauthenticates or transmits.
//
// Pages: Main (List of accumulated APs, strongest first) -> Detail (Text, one AP) or Reset (Confirm).
// Threads: open/view/select/back/key/tick/cancel run on the UI task; call() runs on the worker task.
// Each sweep leases RadioPhase::WifiSurvey only for its duration and releases it between sweeps so the
// uplink and other ops can run. Shared state is copied under modules::Guard, never held across a scan or
// an SD write. Pure logic (the AP table, formatters, CSV escaping) is in oyster_core/wifi_recon.h.
#include <Arduino.h>
#include <SD.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#include "board_support.h"
#include "modules.h"
#include "oyster_core/config.h"
#include "oyster_core/types.h"
#include "oyster_core/wifi_recon.h"
#include "radio_scheduler.h"
#include "wifi_survey.h"

namespace oyster::modules {
namespace {

constexpr uint8_t kCallScan = 1;

constexpr size_t kRowCols = 26;                // visible characters of a main-page row
constexpr uint32_t kScanAcquireWaitMs = 1500;  // wait this long for the radio before "radio busy"
constexpr uint32_t kScanMaxMs = 6000;          // hard stop for one sweep across the channels
constexpr uint32_t kScanStuckMs = 15000;       // a sweep with no result after this long is abandoned
constexpr uint32_t kScanGapMs = 150;           // pause between sweeps (radio released during the gap)
constexpr uint32_t kDetailRedrawMs = 1000;     // redraw the detail page so "N s ago" stays current

const char* const kLogDir = "/oyster";
const char* const kLogPath = "/oyster/wardrive.csv";
// ts is millis since boot: the device has no real-time clock and cannot reach NTP without the backend.
const char* const kLogNote = "# ts = ms since device boot (no clock); 2.4GHz listen-only, RSSI not distance";

enum class Level : uint8_t { Main, Detail, Reset };

struct State {
  Level level = Level::Main;
  bool scanning = false;      // continuous scanning toggled on by the user
  bool scan_busy = false;     // a sweep is queued or running on the worker
  bool radio_busy = false;    // the last acquire failed; shown until the next sweep succeeds
  uint32_t scan_started_ms = 0;
  uint32_t last_done_ms = 0;  // completion time of the last sweep (0 = none yet: first sweep is due)
  uint32_t sweeps = 0;        // completed sweeps
  wifi_recon::ApTable table;  // large: this whole struct is a file-static, never on the stack
  uint8_t sel_bssid[6] = {};  // the AP chosen for the Detail page
  bool sel_valid = false;
  bool sd_present = false;    // a card was mounted when the module last checked
  bool sd_logging = false;    // append each sighting to the CSV (only when a card is present)
  bool sd_error = false;      // a write failed; logging paused
  uint32_t logged = 0;        // CSV lines written this session
};

State s_state;                  // guarded by modules::Guard
std::atomic<bool> s_scan_cancel{false};
WifiSurveyResult s_scan_result;  // worker task only; large, kept off the 16 KiB worker stack
uint32_t s_next_detail_ms = 0;   // UI task only

// ---- worker task -------------------------------------------------------------------------------------------

// Append this sweep's observations to the SD log. Reads the worker-only result buffer, so it holds the
// guard only to sample/update the SD flags, never across the file I/O.
void log_sweep(const WifiSurveyResult& r, uint32_t now) {
  bool want;
  {
    Guard g;
    want = s_state.sd_logging && s_state.sd_present;
  }
  if (!want || r.count == 0) return;
  if (!g_board.sd_ready()) {  // card removed since we last looked
    Guard g;
    s_state.sd_present = false;
    s_state.sd_logging = false;
    return;
  }
  SD.mkdir(kLogDir);  // idempotent; harmless if it already exists
  const bool had = SD.exists(kLogPath);
  File f = SD.open(kLogPath, FILE_APPEND);
  if (!f) {
    g_board.report_sd_write_error();
    Guard g;
    s_state.sd_error = true;
    return;
  }
  if (!had) {  // fresh file: note the clock caveat, then the column header
    f.println(kLogNote);
    f.println(wifi_recon::csv_header());
  }
  uint32_t n = 0;
  char line[96];
  for (uint8_t i = 0; i < r.count; ++i) {
    wifi_recon::format_csv_line(r.aps[i], now, line, sizeof(line));
    f.println(line);
    ++n;
  }
  f.close();
  Guard g;
  s_state.logged += n;
  s_state.sd_error = false;
}

void run_scan() {
  if (!g_radio.acquire(RadioPhase::WifiSurvey, kScanAcquireWaitMs)) {
    {
      Guard g;
      s_state.scan_busy = false;
      s_state.radio_busy = true;
      s_state.last_done_ms = millis();  // back off one gap before retrying
    }
    toast("Radio busy, retrying");
    changed();
    return;
  }
  WifiSurveyConfig cfg_s;
  cfg_s.passive = true;
  cfg_s.show_hidden = true;  // recon wants hidden APs too (logged as hidden)
  cfg_s.channel = 0;         // all 2.4 GHz channels
  cfg_s.ms_per_channel = cfg::kWifiScanMsPerChannel;
  cfg_s.max_results = cfg::kMaxApObservations;
  cfg_s.max_duration_ms = kScanMaxMs;
  if (!g_wifi_survey.start(cfg_s)) {
    g_radio.release(RadioPhase::WifiSurvey);
    Guard g;
    s_state.scan_busy = false;
    s_state.radio_busy = false;
    s_state.last_done_ms = millis();
    return;
  }
  PollState ps = PollState::Running;
  for (;;) {
    ps = g_wifi_survey.poll(s_scan_result);
    if (ps != PollState::Running) break;
    if (s_scan_cancel.load()) g_wifi_survey.cancel();
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  g_radio.release(RadioPhase::WifiSurvey);

  const uint32_t now = millis();
  if (ps == PollState::Done) {
    {
      Guard g;
      for (uint8_t i = 0; i < s_scan_result.count; ++i) s_state.table.merge(s_scan_result.aps[i], now);
      s_state.table.sort_by_rssi();
      ++s_state.sweeps;
      s_state.radio_busy = false;
    }
    log_sweep(s_scan_result, now);  // reads the worker-only buffer; its own short guard for SD flags
  }
  {
    Guard g;
    s_state.scan_busy = false;
    s_state.last_done_ms = now;
  }
  changed();
}

void mod_call(uint8_t call_id, const char* arg) {
  (void)arg;
  if (call_id == kCallScan) run_scan();
}

// ---- requests (UI task) ------------------------------------------------------------------------------------

// Queue the next sweep if continuous scanning is on and the gap since the last one has elapsed.
void kick_scan_if_due(uint32_t now) {
  {
    Guard g;
    if (!s_state.scanning || s_state.scan_busy) return;
    const uint32_t since = now - s_state.last_done_ms;
    if (s_state.last_done_ms != 0 && since < kScanGapMs) return;
    s_state.scan_busy = true;
    s_state.scan_started_ms = now;
  }
  s_scan_cancel.store(false);
  if (!request(Id::WifiRecon, kCallScan)) {
    Guard g;
    s_state.scan_busy = false;  // queue full: tick retries on the next pass
  }
}

// ---- pages (UI task) ---------------------------------------------------------------------------------------

void main_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i >= s_state.table.count()) {
    if (n > 0) out[0] = '\0';
    return;
  }
  wifi_recon::format_row(s_state.table.at(i), out, n, kRowCols);
}

void view_main(PageView& v) {  // caller holds the guard
  v.kind = PageKind::List;
  v.page_id = 0;
  const size_t count = s_state.table.count();
  std::snprintf(v.title, sizeof(v.title), "WiFi %u APs (%s)", static_cast<unsigned>(count),
                s_state.scanning ? "scanning" : "idle");
  v.count = static_cast<uint16_t>(count);
  v.row = main_row;
  v.empty = "No networks yet - press s to scan. 2.4 GHz only.";
  std::snprintf(v.left, sizeof(v.left), "Ent:Detail s:Scan r:Reset");
  std::snprintf(v.right, sizeof(v.right), "%s",
                !s_state.sd_present ? "no SD" : (s_state.sd_error ? "SD err"
                                                 : (s_state.sd_logging ? "l:Log on" : "l:Log off")));
}

void view_detail(PageView& v) {  // caller holds the guard
  v.kind = PageKind::Text;
  v.page_id = 1;
  const int idx = s_state.sel_valid ? s_state.table.find(s_state.sel_bssid) : -1;
  if (idx < 0) {
    std::snprintf(v.title, sizeof(v.title), "AP detail");
    std::snprintf(v.body, sizeof(v.body),
                  "This network is no longer in the table (cleared or evicted). Go back and pick another.");
    std::snprintf(v.left, sizeof(v.left), "Del:Back");
    return;
  }
  const wifi_recon::ApEntry& e = s_state.table.at(static_cast<size_t>(idx));
  char bssid[wifi_recon::kBssidStrLen];
  wifi_recon::bssid_to_str(e.bssid, bssid, sizeof(bssid));
  char ssid[wifi_recon::kSsidBytes + 10];
  wifi_recon::display_ssid(e.ssid, e.ssid_len, ssid, sizeof(ssid));
  const uint32_t now = millis();
  char first[16];
  char last[16];
  wifi_recon::format_relative(now - e.first_seen_ms, first, sizeof(first));
  wifi_recon::format_relative(now - e.last_seen_ms, last, sizeof(last));
  std::snprintf(v.title, sizeof(v.title), "AP %.26s", ssid);
  std::snprintf(v.body, sizeof(v.body),
                "BSSID: %s\nSSID: %s\nChannel: %u (2.4 GHz)\nSecurity: %s\nRSSI: max %d, last %d dBm\n"
                "Seen: %u times\nFirst: %s\nLast: %s\n\nListen-only; RSSI is signal strength, not distance.",
                bssid, ssid, static_cast<unsigned>(e.channel), to_string(e.auth),
                static_cast<int>(e.max_rssi), static_cast<int>(e.last_rssi),
                static_cast<unsigned>(e.times_seen), first, last);
  std::snprintf(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
}

void view_reset(PageView& v) {  // caller holds the guard
  v.kind = PageKind::Confirm;
  v.page_id = 2;
  std::snprintf(v.title, sizeof(v.title), "Clear AP table");
  std::snprintf(v.confirm_operation, sizeof(v.confirm_operation), "Clear AP table");
  std::snprintf(v.confirm_target, sizeof(v.confirm_target), "%u networks in memory",
                static_cast<unsigned>(s_state.table.count()));
  std::snprintf(v.confirm_effect, sizeof(v.confirm_effect),
                "Forgets every AP seen so far. The SD log file is kept. Scanning continues if it was on.");
}

void mod_view(PageView& v) {
  Guard g;
  switch (s_state.level) {
    case Level::Main: view_main(v); break;
    case Level::Detail: view_detail(v); break;
    case Level::Reset: view_reset(v); break;
  }
}

void mod_select(uint16_t row) {
  Level lvl;
  {
    Guard g;
    lvl = s_state.level;
  }
  if (lvl == Level::Main) {
    bool ok = false;
    {
      Guard g;
      if (row < s_state.table.count()) {
        std::memcpy(s_state.sel_bssid, s_state.table.at(row).bssid, 6);
        s_state.sel_valid = true;
        s_state.level = Level::Detail;
        ok = true;
      }
    }
    if (ok) changed();
  } else if (lvl == Level::Reset) {
    {
      Guard g;
      s_state.table.clear();
      s_state.sel_valid = false;
      s_state.level = Level::Main;
    }
    toast("Table cleared");
    changed();
  }
}

bool mod_back() {
  Guard g;
  switch (s_state.level) {
    case Level::Main: return false;  // leave the module
    case Level::Detail: s_state.level = Level::Main; return true;
    case Level::Reset: s_state.level = Level::Main; return true;
  }
  return false;
}

bool mod_key(NavEvent e, char c) {
  if (e != NavEvent::Char) return false;
  Level lvl;
  {
    Guard g;
    lvl = s_state.level;
  }
  if (lvl != Level::Main) return false;
  if (c == 's' || c == 'S') {
    bool now_on;
    {
      Guard g;
      s_state.scanning = !s_state.scanning;
      now_on = s_state.scanning;
      if (now_on) s_state.last_done_ms = 0;  // make the first sweep due immediately
    }
    if (now_on) {
      kick_scan_if_due(millis());
    } else {
      s_scan_cancel.store(true);  // stop the sweep in flight
      toast("Scanning stopped");
    }
    changed();
    return true;
  }
  if (c == 'r' || c == 'R') {
    Guard g;
    s_state.level = Level::Reset;
    changed();
    return true;
  }
  if (c == 'l' || c == 'L') {
    bool present;
    bool on = false;
    {
      Guard g;
      present = s_state.sd_present;
      if (present) {
        s_state.sd_logging = !s_state.sd_logging;
        on = s_state.sd_logging;
      }
    }
    toast(present ? (on ? "SD logging on" : "SD logging off") : "No SD card");
    changed();
    return true;
  }
  return false;
}

bool mod_tick(uint32_t now_ms) {
  bool dirty = false;
  Level lvl;
  {
    Guard g;
    lvl = s_state.level;
    if (s_state.scan_busy && (now_ms - s_state.scan_started_ms) > kScanStuckMs) {
      s_state.scan_busy = false;
      s_state.scanning = false;
      s_state.last_done_ms = now_ms;
      dirty = true;
    }
  }
  kick_scan_if_due(now_ms);
  if (lvl == Level::Detail && static_cast<int32_t>(now_ms - s_next_detail_ms) >= 0) {
    s_next_detail_ms = now_ms + kDetailRedrawMs;  // keep the relative times fresh
    dirty = true;
  }
  return dirty;
}

void mod_cancel() {
  {
    Guard g;
    s_state.scanning = false;
  }
  s_scan_cancel.store(true);  // the worker's poll loop cancels and releases the radio
  changed();
}

void mod_open() {
  const bool sd = g_board.sd_ready();
  {
    Guard g;
    s_state.level = Level::Main;
    s_state.scanning = false;
    s_state.scan_busy = false;
    s_state.radio_busy = false;
    s_state.last_done_ms = 0;
    s_state.sweeps = 0;
    s_state.table.clear();
    s_state.sel_valid = false;
    s_state.sd_present = sd;
    s_state.sd_logging = sd;  // on by default when a card is present
    s_state.sd_error = false;
    s_state.logged = 0;
  }
  s_scan_cancel.store(false);
  s_next_detail_ms = millis();
}

bool mod_available(const char** why) {
  (void)why;
  return true;
}

void mod_tile_status(char* out, size_t n) {
  Guard g;
  if (s_state.scanning) {
    std::snprintf(out, n, "scan");
  } else {
    std::snprintf(out, n, "%u APs", static_cast<unsigned>(s_state.table.count()));
  }
}

}  // namespace

const Ops kWifiReconOps = {"WiFi Scan", mod_available, mod_open,   mod_view,        mod_select, mod_back,
                           mod_key,     mod_tick,      mod_cancel, mod_call,        mod_tile_status,
                           nullptr};

}  // namespace oyster::modules
