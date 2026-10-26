// Wi-Fi setup module (Settings > Wi-Fi): join a 2.4 GHz network from the device, no PC needed.
// Pages: Status (current/saved network, link, backend) -> Scan (list of networks) -> Password (entry)
// or Forget (confirm). The scan runs on the worker task through modules::request, leasing
// RadioPhase::WifiSurvey so the uplink pauses, exactly like the survey operation; it is never recorded
// as evidence. Joining writes the SSID + passphrase to DeviceConfig (the backend address, token and CA
// are untouched) and invalidates the backend credentials so the link re-reads the configuration, the
// same hand-off serial provisioning makes on DONE. The passphrase is never logged or shown.
//
// Threads: open/view/select/back/key/tick/cancel/entry run on the UI task; call() runs on the worker
// task. Shared state is copied under modules::Guard, never held across the scan. The scan itself blocks
// the worker task (like an HTTP request) while polling; the UI task stays responsive and shows "Scanning".
// Pure selection logic (dedupe/sort/bounds, display sanitising) is in oyster_core/wifi_pick.h.
#include <Arduino.h>
#include <WiFi.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#include "backend_client.h"
#include "device_config.h"
#include "modules.h"
#include "net_link.h"
#include "oyster_core/config.h"
#include "oyster_core/wifi_pick.h"
#include "radio_scheduler.h"
#include "wifi_survey.h"

namespace oyster::modules {
namespace {

constexpr uint8_t kCallScan = 1;
constexpr uint8_t kCallStatus = 2;

constexpr size_t kRowCols = 26;             // characters of a scan-list row
constexpr uint32_t kStatusPeriodMs = 2000;  // status page refresh cadence
constexpr uint32_t kScanAcquireWaitMs = 1500;  // wait this long for the radio before "radio busy"
constexpr uint32_t kScanMaxMs = 8000;       // hard stop for one scan
constexpr uint32_t kScanStuckMs = 20000;    // a scan with no result after this long is treated as failed
constexpr size_t kWifiPwMin = 8;            // WPA passphrase minimum
constexpr size_t kWifiPwMax = 63;           // WPA passphrase maximum (hex PSK of 64 is not entered here)

enum class Level : uint8_t { Status, Scan, Password, Forget };

struct State {
  Level level;
  // Saved configuration snapshot (refreshed on open and after a join/forget).
  char saved_ssid[33];
  bool configured;
  // Live link snapshot, sampled on the worker task.
  bool link_valid;
  bool link_up;
  int8_t link_rssi;
  char cur_ssid[33];
  char ip[16];
  // Scan state.
  bool scan_busy;
  bool scan_valid;
  bool scan_failed;
  bool scan_radio_busy;
  bool scan_cancelled;
  bool scan_timed_out;
  uint32_t scan_started_ms;
  wifi_pick::PickList picks;
  // Network chosen on the scan list, carried into the password/join step.
  uint8_t sel_ssid[33];
  uint8_t sel_ssid_len;
  bool sel_open;
};

State s_state;                       // guarded by modules::Guard
std::atomic<bool> s_scan_cancel{false};
WifiSurveyResult s_scan_result;      // worker task only; large, kept out of the stack
uint32_t s_next_status_ms = 0;       // UI task only

uint32_t elapsed_ms(uint32_t now, uint32_t since) {
  const int32_t d = static_cast<int32_t>(now - since);
  return d < 0 ? 0u : static_cast<uint32_t>(d);
}

// ---- requests (UI task) ------------------------------------------------------------------------------------

void start_scan() {
  {
    Guard g;
    s_state.level = Level::Scan;
    if (s_state.scan_busy) {
      toast("Already scanning");
      return;
    }
    s_state.scan_busy = true;
    s_state.scan_failed = false;
    s_state.scan_radio_busy = false;
    s_state.scan_cancelled = false;
    s_state.scan_timed_out = false;
    s_state.scan_started_ms = millis();
  }
  s_scan_cancel.store(false);
  if (!request(Id::WifiSetup, kCallScan)) {
    {
      Guard g;
      s_state.scan_busy = false;
      s_state.scan_failed = true;
    }
    toast("Busy, try again");
  }
  changed();
}

void request_status() {
  if (!request(Id::WifiSetup, kCallStatus)) return;  // a stale snapshot stays on screen
}

// Persist a chosen network and hand the change to the link, the same way serial provisioning does on DONE.
// Runs on the UI task: DeviceConfig has its own lock and the writes are flash, not network.
void do_join(const char* ssid, const char* password) {
  DeviceConfig c{};
  g_config.load(c);
  std::snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", ssid);
  bool ok = g_config.save(c);
  if (ok) ok = g_config.set_wifi_password(password);  // "" = open network
  if (!ok) {
    toast("Save failed");
    return;
  }
  // Bump the configuration so the link and backend re-read it. invalidate_credentials() is any-task-safe
  // and drops the cached token/CA/connection; the station picks up the new SSID on its next attempt.
  g_backend.invalidate_credentials();
  net::reconfigure();  // drop any association to the old AP so the link re-joins with the new SSID
  char msg[40];
  std::snprintf(msg, sizeof(msg), "Joining %.24s", ssid);
  toast(msg);
  {
    Guard g;
    s_state.level = Level::Status;
    std::snprintf(s_state.saved_ssid, sizeof(s_state.saved_ssid), "%s", ssid);
    s_state.configured = ssid[0] != '\0';
    s_state.link_valid = false;
  }
  s_next_status_ms = millis();
  request_status();
  changed();
}

void do_forget() {
  DeviceConfig c{};
  g_config.load(c);
  c.wifi_ssid[0] = '\0';
  bool ok = g_config.save(c);
  if (ok) ok = g_config.set_wifi_password("");  // overwrite the stored passphrase
  if (!ok) {
    toast("Could not forget");
    return;
  }
  g_backend.invalidate_credentials();
  net::reconfigure();  // drop the association so the device goes offline promptly after forgetting
  toast("Network forgotten");
  {
    Guard g;
    s_state.level = Level::Status;
    s_state.saved_ssid[0] = '\0';
    s_state.configured = false;
    s_state.link_valid = false;
  }
  s_next_status_ms = millis();
  request_status();
  changed();
}

// ---- worker task -------------------------------------------------------------------------------------------

void run_status() {
  const bool up = net::up();
  const int8_t rssi = up ? net::rssi() : 0;
  char ssid[33] = "";
  char ip[16] = "";
  if (up) {
    std::snprintf(ssid, sizeof(ssid), "%s", WiFi.SSID().c_str());
    const IPAddress a = WiFi.localIP();
    std::snprintf(ip, sizeof(ip), "%u.%u.%u.%u", static_cast<unsigned>(a[0]), static_cast<unsigned>(a[1]),
                  static_cast<unsigned>(a[2]), static_cast<unsigned>(a[3]));
  }
  {
    Guard g;
    s_state.link_up = up;
    s_state.link_rssi = rssi;
    std::snprintf(s_state.cur_ssid, sizeof(s_state.cur_ssid), "%s", ssid);
    std::snprintf(s_state.ip, sizeof(s_state.ip), "%s", ip);
    s_state.link_valid = true;
  }
  changed();
}

void run_scan() {
  if (!g_radio.acquire(RadioPhase::WifiSurvey, kScanAcquireWaitMs)) {
    {
      Guard g;
      s_state.scan_busy = false;
      s_state.scan_radio_busy = true;
    }
    toast("Radio busy, try again");
    changed();
    return;
  }
  WifiSurveyConfig cfg_s;
  cfg_s.passive = true;
  cfg_s.show_hidden = false;  // hidden networks cannot be joined by name from the list
  cfg_s.channel = 0;          // all 2.4 GHz channels
  cfg_s.ms_per_channel = cfg::kWifiScanMsPerChannel;
  cfg_s.max_results = cfg::kMaxApObservations;
  cfg_s.max_duration_ms = kScanMaxMs;
  if (!g_wifi_survey.start(cfg_s)) {
    g_radio.release(RadioPhase::WifiSurvey);
    {
      Guard g;
      s_state.scan_busy = false;
      s_state.scan_failed = true;
    }
    toast("Scan failed");
    changed();
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
  const bool timed_out = s_scan_result.timed_out;
  {
    Guard g;
    s_state.scan_busy = false;
    if (ps == PollState::Done) {
      wifi_pick::build(s_scan_result.aps, s_scan_result.count, s_state.picks);
      s_state.scan_valid = true;
      s_state.scan_failed = false;
      s_state.scan_timed_out = false;
    } else if (ps == PollState::Cancelled) {
      s_state.scan_cancelled = true;
    } else {
      s_state.scan_failed = true;
      s_state.scan_timed_out = timed_out;
    }
  }
  if (ps == PollState::Cancelled) {
    toast("Scan cancelled");
  } else if (ps != PollState::Done) {
    toast(timed_out ? "Scan timed out" : "Scan failed");
  }
  changed();
}

void mod_call(uint8_t call_id, const char* arg) {
  (void)arg;
  if (call_id == kCallScan) {
    run_scan();
  } else if (call_id == kCallStatus) {
    run_status();
  }
}

// ---- pages (UI task) ---------------------------------------------------------------------------------------

void scan_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i >= s_state.picks.count) {
    if (n > 0) out[0] = '\0';
    return;
  }
  wifi_pick::format_row(s_state.picks.items[i], out, n, kRowCols);
}

void view_status(PageView& v) {  // caller holds the guard
  v.kind = PageKind::Text;
  std::snprintf(v.title, sizeof(v.title), "Wi-Fi");
  char* b = v.body;
  size_t cap = sizeof(v.body);
  int w = std::snprintf(b, cap, "Saved: %s\n", s_state.configured ? s_state.saved_ssid : "(none)");
  size_t used = (w > 0 && static_cast<size_t>(w) < cap) ? static_cast<size_t>(w) : 0;
  if (!s_state.link_valid) {
    std::snprintf(b + used, cap - used, "Checking link...\n");
  } else if (s_state.link_up) {
    std::snprintf(b + used, cap - used, "Joined: %s\nIP: %s\nSignal: %d dBm\nBackend: %s\n",
                  s_state.cur_ssid[0] != '\0' ? s_state.cur_ssid : "(unknown)", s_state.ip,
                  static_cast<int>(s_state.link_rssi), to_string(g_backend.state()));
  } else {
    std::snprintf(b + used, cap - used, "Not joined.\nBackend: %s\n%s",
                  to_string(g_backend.state()),
                  s_state.configured ? "The saved network is not in range or the password is wrong."
                                     : "No network saved. Scan to join one.");
  }
  std::snprintf(v.left, sizeof(v.left), "Ent:Scan Del:Back");
  std::snprintf(v.right, sizeof(v.right), "%s", s_state.configured ? "f:Forget" : "");
}

void view_scan(PageView& v) {  // caller holds the guard
  v.kind = PageKind::List;
  std::snprintf(v.title, sizeof(v.title), "Wi-Fi networks");
  v.busy = s_state.scan_busy;
  if (s_state.scan_busy) {
    v.count = 0;
    v.empty = "Scanning...";
    std::snprintf(v.left, sizeof(v.left), "Scanning...");
    std::snprintf(v.right, sizeof(v.right), "Esc:Stop");
    return;
  }
  if (!s_state.scan_valid) {
    v.count = 0;
    v.empty = s_state.scan_radio_busy ? "Radio busy. r: try again"
                                      : (s_state.scan_cancelled ? "Scan cancelled. r: rescan"
                                                                : "Scan failed. r: rescan");
    std::snprintf(v.left, sizeof(v.left), "r:Rescan Del:Back");
    return;
  }
  v.count = s_state.picks.count;
  v.row = scan_row;
  v.empty = "No networks found. r: rescan";
  std::snprintf(v.left, sizeof(v.left), "r:Rescan Del:Back");
  std::snprintf(v.right, sizeof(v.right), "Ent:Join");
}

void view_password(PageView& v) {  // caller holds the guard
  v.kind = PageKind::Entry;
  std::snprintf(v.title, sizeof(v.title), "Wi-Fi password");
  char name[wifi_pick::kSsidBufLen + 1];
  wifi_pick::display_ssid(s_state.sel_ssid, s_state.sel_ssid_len, name, sizeof(name));
  std::snprintf(v.entry_prompt, sizeof(v.entry_prompt), "Password for %.26s", name);
  v.entry_initial[0] = '\0';
  v.entry_max = static_cast<uint8_t>(kWifiPwMax);
  v.entry_secret = true;
  std::snprintf(v.left, sizeof(v.left), "Ent:Join Del:Back");
}

void view_forget(PageView& v) {  // caller holds the guard
  v.kind = PageKind::Confirm;
  std::snprintf(v.title, sizeof(v.title), "Forget Wi-Fi");
  std::snprintf(v.confirm_operation, sizeof(v.confirm_operation), "Forget network");
  std::snprintf(v.confirm_target, sizeof(v.confirm_target), "%.39s",
                s_state.configured ? s_state.saved_ssid : "(none)");
  std::snprintf(v.confirm_effect, sizeof(v.confirm_effect),
                "Clears the saved network and password. The device goes offline until you join a network.");
}

void mod_view(PageView& v) {
  Guard g;
  v.page_id = static_cast<uint8_t>(s_state.level);
  switch (s_state.level) {
    case Level::Status: view_status(v); break;
    case Level::Scan: view_scan(v); break;
    case Level::Password: view_password(v); break;
    case Level::Forget: view_forget(v); break;
  }
}

void select_network(uint16_t row) {
  char ssid[33] = "";
  bool open = false;
  bool ok = false;
  {
    Guard g;
    if (row < s_state.picks.count) {
      const wifi_pick::Pick& p = s_state.picks.items[row];
      std::memcpy(s_state.sel_ssid, p.ssid, p.ssid_len);
      s_state.sel_ssid[p.ssid_len] = '\0';
      s_state.sel_ssid_len = p.ssid_len;
      s_state.sel_open = p.open;
      std::memcpy(ssid, p.ssid, p.ssid_len);
      ssid[p.ssid_len] = '\0';
      open = p.open;
      ok = true;
      if (!open) s_state.level = Level::Password;  // secured: ask for the passphrase
    }
  }
  if (!ok) return;
  if (open) do_join(ssid, "");  // open network: no passphrase page
  changed();
}

void mod_select(uint16_t row) {
  Level lvl;
  {
    Guard g;
    lvl = s_state.level;
  }
  switch (lvl) {
    case Level::Status: start_scan(); break;    // Enter on the status text = scan
    case Level::Scan: select_network(row); break;
    case Level::Password: break;                // handled by entry()
    case Level::Forget: do_forget(); break;     // Enter approves the confirmation
  }
}

void mod_entry(const char* text) {
  Level lvl;
  bool open;
  char ssid[33];
  {
    Guard g;
    lvl = s_state.level;
    open = s_state.sel_open;
    std::memcpy(ssid, s_state.sel_ssid, s_state.sel_ssid_len);
    ssid[s_state.sel_ssid_len] = '\0';
  }
  if (lvl != Level::Password) return;
  if (open) {  // should not happen: open networks skip this page
    do_join(ssid, "");
    return;
  }
  const size_t len = std::strlen(text);
  if (len < kWifiPwMin || len > kWifiPwMax) {
    toast("Password 8-63 characters");
    return;  // stay on the entry page
  }
  do_join(ssid, text);
}

bool mod_back() {
  Guard g;
  switch (s_state.level) {
    case Level::Status: return false;  // leave the module
    case Level::Scan: s_state.level = Level::Status; return true;
    case Level::Password: s_state.level = Level::Scan; return true;
    case Level::Forget: s_state.level = Level::Status; return true;
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
  if ((c == 'r' || c == 'R') && lvl == Level::Scan) {
    start_scan();
    return true;
  }
  if ((c == 'f' || c == 'F') && lvl == Level::Status) {
    bool has;
    {
      Guard g;
      has = s_state.configured;
      if (has) s_state.level = Level::Forget;
    }
    if (!has) toast("No network saved");
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
    if (s_state.scan_busy && elapsed_ms(now_ms, s_state.scan_started_ms) > kScanStuckMs) {
      s_state.scan_busy = false;
      s_state.scan_failed = true;
      dirty = true;
    }
  }
  if (lvl == Level::Status && static_cast<int32_t>(now_ms - s_next_status_ms) >= 0) {
    s_next_status_ms = now_ms + kStatusPeriodMs;
    request_status();
  }
  return dirty;
}

void mod_cancel() {
  s_scan_cancel.store(true);
  changed();
}

void mod_open() {
  DeviceConfig c{};
  g_config.load(c);
  {
    Guard g;
    s_state.level = Level::Status;
    std::snprintf(s_state.saved_ssid, sizeof(s_state.saved_ssid), "%s", c.wifi_ssid);
    s_state.configured = c.wifi_ssid[0] != '\0';
    s_state.link_valid = false;
    s_state.scan_busy = false;
    s_state.scan_valid = false;
    s_state.scan_failed = false;
    s_state.scan_radio_busy = false;
    s_state.scan_cancelled = false;
    s_state.picks = wifi_pick::PickList{};
    s_state.sel_ssid_len = 0;
    s_state.sel_open = false;
  }
  s_scan_cancel.store(false);
  s_next_status_ms = millis() + kStatusPeriodMs;
  request_status();  // sample the link now; tick() refreshes it every 2 s on the status page
}

bool mod_available(const char** why) {
  (void)why;
  return true;
}

void mod_tile_status(char* out, size_t n) {
  if (n > 0) out[0] = '\0';  // opened from Settings, not from Home
}

}  // namespace

const Ops kWifiSetupOps = {"Wi-Fi",    mod_available, mod_open,   mod_view,        mod_select, mod_back,
                           mod_key,    mod_tick,      mod_cancel, mod_call,        mod_tile_status,
                           mod_entry};

}  // namespace oyster::modules
