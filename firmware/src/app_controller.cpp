// AppController (app_controller.h) and the worker -> UI event queue (app_events.h).
// Runs in the Arduino loop task only. Radio, network and backend work is always queued to the
// AssessmentRunner (g_runner.enqueue); the loop never waits on it. Short NVS/SD calls happen only at
// boot, after USB provisioning ends and on "New session".
// Emergency cancel (Fn+`, Fn+Esc, Ctrl+C) is handled before any per-screen logic and acknowledged on
// the footer in the same loop iteration.
#include "app_controller.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include <cctype>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "app_events.h"
#include "assessment_runner.h"
#include "assets.h"
#include "backend_client.h"
#include "board_support.h"
#include "device_config.h"
#include "evidence_store.h"
#include "modules.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "oyster_core/lab_profile_gen.h"
#include "radio_scheduler.h"
#include "results_cache.h"
#include "serial_provisioning.h"
#include "status_led.h"
#include "telemetry.h"
#include "ui.h"
#include "voice_capture.h"

namespace oyster {


AppController g_app;

// ===== Worker -> UI event queue =================================================================

namespace {

constexpr UBaseType_t kUiQueueDepth = 16;
QueueHandle_t volatile s_ui_queue = nullptr;
volatile uint32_t s_ui_dropped = 0;
portMUX_TYPE s_queue_mux = portMUX_INITIALIZER_UNLOCKED;

// Created on first use so modules started before the UI can already post.
QueueHandle_t ui_queue() {
  if (s_ui_queue != nullptr) return s_ui_queue;
  QueueHandle_t q = xQueueCreate(kUiQueueDepth, sizeof(AppEvent));
  if (q == nullptr) return nullptr;
  portENTER_CRITICAL(&s_queue_mux);
  if (s_ui_queue == nullptr) {
    s_ui_queue = q;
    q = nullptr;
  }
  portEXIT_CRITICAL(&s_queue_mux);
  if (q != nullptr) vQueueDelete(q);
  return s_ui_queue;
}

void copy_str(char* dst, size_t n, const char* src) {
  if (dst == nullptr || n == 0) return;
  size_t i = 0;
  if (src != nullptr) {
    for (; i + 1 < n && src[i] != '\0'; ++i) dst[i] = src[i];
  }
  dst[i] = '\0';
}

}  // namespace

bool app_post_event(const AppEvent& ev) {
  QueueHandle_t q = ui_queue();
  if (q == nullptr || xQueueSend(q, &ev, 0) != pdTRUE) {
    __atomic_fetch_add(&s_ui_dropped, 1u, __ATOMIC_RELAXED);
    return false;
  }
  return true;
}

bool app_post(AppEventType type, uint32_t a, uint32_t b, const char* text) {
  AppEvent ev{};
  ev.type = type;
  ev.a = a;
  ev.b = b;
  copy_str(ev.text, sizeof(ev.text), text);
  return app_post_event(ev);
}

// ===== Controller state ==========================================================================

namespace {

constexpr uint16_t kListRows = ui::kRows;  // the title is the heading above the rows
constexpr uint16_t kMsgRows = ui::kRows;
constexpr uint32_t kToastMs = 3000;
constexpr uint32_t kCancelAckMs = 3000;
constexpr uint32_t kConfirmGuardMs = 700;  // keys pressed just before a dialog appears never answer it
constexpr uint32_t kPollMs = 250;
constexpr uint32_t kLiveRefreshMs = 1000;
constexpr uint32_t kStatusRefreshMs = 2000;
constexpr uint32_t kRequestStartTimeoutMs = 5000;
constexpr uint32_t kStaleOpMs = 3000;
constexpr int kScreenCount = static_cast<int>(Screen::KeyTest) + 1;
constexpr uint32_t kKeyTestLeaveMs = 2000;  // Key tester: a second Back/Home within this time leaves
constexpr uint8_t kBrightness[] = {24, 64, 128, 192, 255};
constexpr uint8_t kBrightnessLevels = sizeof(kBrightness);

struct ListNav {
  uint16_t sel = 0;
  uint16_t top = 0;
};

enum class EntryPurpose : uint8_t { RoomLabel, Question, Module };
enum class Seen : uint8_t { NoScan, NotSeen, ByAddress, ByUuid, ByName };
enum class AItem : uint8_t { Target, Inspect, Read, GattResults, LastRead, NotInBuild };

struct AssessItem {
  AItem kind;
  uint8_t idx;
};

struct OpTrack {
  bool active;              // between OperationStarted and OperationFinished
  bool requested;           // queued from the UI, not yet started
  Operation op;
  uint32_t items;
  uint32_t started_ms;
  uint32_t requested_ms;
  char label[64];
};

// Home (H01): the OysterOS modules as a 2-column tile grid, row-major. Numbers follow the H01 wireframe
// (column 1: 1-3, column 2: 4-6), then 7 Settings and 8 Help in a fourth row (scrolled into view).
enum class HomeItem : uint8_t { Security, ControlDeck, OysterAi, SmartHome, WifiScan, OriginalApps, Settings, Help };
struct HomeTile {
  HomeItem item;
  uint8_t number;
  const char* label;
  assets::IconId icon;  // IconId::Count = no bitmap ('?' glyph)
};
const HomeTile kHome[] = {
    {HomeItem::Security, 1, "Security", assets::IconId::Security},
    {HomeItem::ControlDeck, 4, "Control", assets::IconId::ControlDeck},
    {HomeItem::OysterAi, 2, "Oyster AI", assets::IconId::OysterAi},
    {HomeItem::SmartHome, 5, "SmartHome", assets::IconId::SmartHome},
    {HomeItem::WifiScan, 3, "WiFi Scan", assets::IconId::Wifi},
    {HomeItem::OriginalApps, 6, "Original", assets::IconId::Apps},
    {HomeItem::Settings, 7, "Settings", assets::IconId::Settings},
    {HomeItem::Help, 8, "Help", assets::IconId::Count},
};
constexpr uint8_t kHomeCount = sizeof(kHome) / sizeof(kHome[0]);
constexpr uint8_t kHomeCols = 2;

// Modules of the design that this firmware does not implement (design constraint #5: visibly unavailable).
struct UnavailableModule {
  const char* title;
  const char* body;
};
const UnavailableModule kUnavailable[] = {
    {"Linux: unavailable",
     "Not available in this build.\nHost health, services and logs need a companion host adapter that the "
     "backend does not provide. Nothing on this screen runs or connects."},
    {"Control Deck: unavailable",
     "Not available in this build.\nMacro profiles and a job runner for them do not exist on the device or "
     "the backend. No action can be started from here."},
    {"Smart Home: unavailable",
     "Not available in this build.\nThere are no IR profiles and no Home Assistant integration, so no device "
     "state is shown and nothing is switched."},
    {"Original Apps: unavailable",
     "Not available in this build.\nThis firmware cannot start the original launcher or its apps and does not "
     "imitate them. That needs a verified launcher integration (docs/OysterOS_Project_Plan.md section 1)."},
};
constexpr uint8_t kUnavailableCount = sizeof(kUnavailable) / sizeof(kUnavailable[0]);

// Security (S10): the implemented module, as a 1-column tile list.
const char* const kSecLabels[] = {"Wi-Fi Survey", "Bluetooth Explorer", "Saved Findings", "Enrolled Targets",
                                  "Assess (GATT)"};
const assets::IconId kSecIcons[] = {assets::IconId::Wifi, assets::IconId::Ble, assets::IconId::Findings,
                                    assets::IconId::Targets, assets::IconId::Assess};
const Screen kSecScreens[] = {Screen::WifiSurvey, Screen::BleExplorer, Screen::Evidence, Screen::Targets,
                              Screen::Assess};
constexpr uint8_t kSecCount = sizeof(kSecScreens) / sizeof(kSecScreens[0]);

constexpr uint16_t kWifiMenuCount = 3;
constexpr uint16_t kBleMenuCount = 3;
constexpr uint16_t kAskCount = 5;
constexpr uint16_t kSettingsCount = 10;
const char* const kM4Names[] = {"Access-policy check", "Observe notifications", "Write lab value",
                                "Diagnostic transfer"};

bool s_runtime = false;  // radio/backend/runner were started (Cardputer ADV only)
uint8_t s_start_fail = 0;  // StartupFailure bits reported by main.cpp (+ the UI queue, checked in begin())
uint32_t s_cfg_gen = 0;    // g_provisioning.config_generation() last applied to s_cfg
Screen s_cur = Screen::Boot;
Screen s_stack[12];
uint8_t s_depth = 0;
InputMapper s_input;

bool s_dirty_content = true;
bool s_dirty_footer = true;
bool s_dirty_status = true;
uint32_t s_last_status_ms = 0;
uint32_t s_last_footer_ms = 0;
uint32_t s_last_live_ms = 0;
uint32_t s_last_poll_ms = 0;
bool s_prev_busy = false;
ModeBanner s_prev_mode = ModeBanner::Offline;
BackendState s_prev_backend = BackendState::Offline;
bool s_footer_timer_active = false;

char s_session[cfg::kIdBufLen] = {};
bool s_session_ok = false;
char s_device_id[cfg::kIdBufLen] = {};
bool s_device_id_fallback = false;
DeviceConfig s_cfg{};
bool s_cfg_ok = false;
char s_room[cfg::kMaxRoomLabelLen + 1] = {};

EnrolledTarget s_targets[cfg::kMaxEnrolledTargets];
uint8_t s_target_count = 0;
Seen s_seen[cfg::kMaxEnrolledTargets];
uint32_t s_seen_version = 0xFFFFFFFFu;
uint8_t s_assess_target = 0;

uint32_t s_cancel_ack_until = 0;
char s_toast[64] = {};
uint32_t s_toast_until = 0;
OpTrack s_op{};
uint32_t s_op_idle_since = 0;
char s_last_result[64] = "none yet";

EntryPurpose s_entry_purpose = EntryPurpose::RoomLabel;
char s_entry[cfg::kMaxQuestionLen + 1] = {};
size_t s_entry_len = 0;
size_t s_entry_cursor = 0;
size_t s_entry_max = 0;

char s_detail_title[40] = {};
char s_detail_body[900] = {};
void (*s_detail_builder)(char*, size_t) = nullptr;
char s_live_body[1500] = {};  // run view: status lines + up to 1200 characters of answer
uint16_t s_scroll = 0;
uint16_t s_msg_total = 0;

PendingConfirmation s_pc{};
bool s_pc_shown = false;
uint32_t s_pc_fetched_ms = 0;
uint32_t s_pc_guard_until = 0;
uint32_t s_pc_last_secs = 0;
char s_resolved_job[cfg::kIdBufLen] = {};
char s_resolved_nonce[cfg::kIdBufLen] = {};

ListNav s_nav[kScreenCount];
ApObservation s_wifi_page[kListRows];
size_t s_wifi_n = 0;
WifiSummary s_wifi_sum{};
BleAdvObservation s_ble_page[8];  // list page (5) or sighting scan (8)
size_t s_ble_n = 0;
BleSummary s_ble_sum{};
GattEntry s_gatt_page[kListRows];
size_t s_gatt_n = 0;
GattSummary s_gatt_sum{};
uint16_t s_page_start = 0;
uint16_t s_count_wifi = 0, s_count_ble = 0, s_count_gatt = 0;
uint32_t s_ver[4] = {0, 0, 0, 0};
WifiSort s_wifi_sort = WifiSort::Rssi;
uint8_t s_brightness_idx = 2;

uint8_t s_lab_char_uuid[lab::kCharCount][16];
bool s_lab_char_ok[lab::kCharCount] = {};
uint8_t s_lab_svc_uuid[16];
bool s_lab_svc_ok = false;

AssessItem s_aitems[4 + lab::kCharCount + 4];
uint8_t s_acount = 0;

uint8_t s_unavailable = 0;  // kUnavailable index shown by Screen::Unavailable
// Module screens (src/modules): the module that is open, the page it last reported, and the key guard of a
// confirmation page.
modules::Id s_module = modules::Id::Hosts;
modules::PageView s_view;
uint8_t s_view_page = 0xFF;
uint32_t s_module_guard_until = 0;
// Status LED attention: an AI answer that has not been opened yet, and the run status it was seen with.
bool s_answer_unseen = false;
char s_run_seen_status[24] = {};
constexpr uint8_t kLegendCount = 10;  // rows of the LED legend (table kLegend below)
Screen s_help_from = Screen::MainMenu;
uint16_t s_help_saved_scroll = 0;

// Settings > Key tester (P11): last physical key change and the logical actions it resolved to.
struct KeyTestState {
  char keys[48];
  char action[48];
  char before[48];
  uint16_t presses;
  NavEvent armed;  // Back or Home seen once; the same again within kKeyTestLeaveMs leaves the tester
  uint32_t armed_until;
};
KeyTestState s_kt{};

// ---- small helpers ------------------------------------------------------------------------------

inline int sidx(Screen s) { return static_cast<int>(s); }

bool before(uint32_t now, uint32_t deadline) { return static_cast<int32_t>(deadline - now) > 0; }

struct Body {
  char* buf;
  size_t cap;
  size_t len;
  void add(const char* fmt, ...) __attribute__((format(printf, 2, 3)));
};

void Body::add(const char* fmt, ...) {
  if (cap == 0 || len + 1 >= cap) return;
  va_list ap;
  va_start(ap, fmt);
  const int w = std::vsnprintf(buf + len, cap - len, fmt, ap);
  va_end(ap);
  if (w > 0) len = (len + static_cast<size_t>(w) < cap - 1) ? len + static_cast<size_t>(w) : cap - 1;
}

Body body_of(char* out, size_t n) {
  if (n > 0) out[0] = '\0';
  return Body{out, n, 0};
}

const char* yes_no_unknown(TriBool t) { return t.known() ? (t.is_true() ? "yes" : "no") : "unknown"; }

const char* status_name(uint32_t v) {
  return v <= static_cast<uint32_t>(EventStatus::Inconclusive) ? to_string(static_cast<EventStatus>(v)) : "?";
}

const char* op_name(Operation op) {
  return static_cast<uint8_t>(op) <= static_cast<uint8_t>(Operation::Unknown) ? to_string(op) : "?";
}

const char* strip_prefix(const char* s, const char* prefix) {
  const size_t n = std::strlen(prefix);
  return std::strncmp(s, prefix, n) == 0 ? s + n : s;
}

void toast(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
void toast(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(s_toast, sizeof(s_toast), fmt, ap);
  va_end(ap);
  s_toast_until = millis() + kToastMs;
  s_dirty_footer = true;
}

void mark_all() { s_dirty_content = s_dirty_footer = s_dirty_status = true; }

bool runner_busy() { return s_runtime && g_runner.busy(); }

// A radio operation (UI- or job-initiated) or a UI request is in flight, per the operation events.
bool op_in_flight() { return s_op.active || s_op.requested; }

// Backend reachable and authenticated. This says nothing about the AI provider: a run can still end as
// ai_unavailable (see latest_run_used_provider).
bool ai_available() { return s_runtime && g_backend.state() == BackendState::Connected; }

// Positive evidence that the latest AI run used a provider: it is running, waiting on a tool, or ended after
// the model was called. queued/ai_unavailable/failed/cancelled/expired are not evidence of a working provider.
bool latest_run_used_provider() {
  char st[sizeof(RunSummary::status)];
  std::memcpy(st, g_backend.latest_run().status, sizeof(st));  // display copy; the worker may update it
  st[sizeof(st) - 1] = '\0';
  static const char* const kUsed[] = {"running", "awaiting_device", "awaiting_local_confirmation", "succeeded",
                                      "budget_exhausted"};
  for (const char* s : kUsed) {
    if (std::strcmp(st, s) == 0) return true;
  }
  return false;
}

ModeBanner current_mode() {
  // Firmware data is always live radio data; SIMULATED is never shown for it.
  if (s_runtime) {
    const RadioPhase p = g_radio.active();
    if (p == RadioPhase::WifiSurvey || p == RadioPhase::BleDiscovery || p == RadioPhase::BleSession) {
      return ModeBanner::LiveRadio;
    }
  }
  if (s_start_fail != 0) return ModeBanner::StartFailed;
  if (s_runtime && g_backend.state() == BackendState::Connected) {
    return latest_run_used_provider() ? ModeBanner::AiConnected : ModeBanner::BackendOk;
  }
  return ModeBanner::Offline;
}

const char* backend_short() {
  if (!s_runtime) return "stopped";
  if ((s_start_fail & (kStartBackend | kStartRunner)) != 0) return "init-err";
  if (!s_cfg_ok) return "cfg-err";
  if (!s_cfg.provisioned) return "unprov";
  switch (g_backend.state()) {
    case BackendState::Offline: return "offline";
    case BackendState::Connecting: return "connect";
    case BackendState::ClockNotSet: return "no-clock";
    case BackendState::CertError: return "cert-err";
    case BackendState::AuthError: return "auth-err";
    case BackendState::Connected: return "online";
  }
  return "?";
}

// Connection icon of the status bar; same sources as backend_short().
ui::Conn backend_conn() {
  if (!s_runtime) return ui::Conn::Offline;
  if ((s_start_fail & (kStartBackend | kStartRunner)) != 0 || !s_cfg_ok) return ui::Conn::Error;
  if (!s_cfg.provisioned) return ui::Conn::Offline;
  switch (g_backend.state()) {
    case BackendState::Offline: return ui::Conn::Offline;
    case BackendState::Connecting: return ui::Conn::Connecting;
    case BackendState::ClockNotSet:
    case BackendState::CertError:
    case BackendState::AuthError: return ui::Conn::Error;
    case BackendState::Connected: return ui::Conn::Online;
  }
  return ui::Conn::Unknown;
}

// Untrusted SSID / BLE name bytes -> display text (never assumed to be UTF-8).
void bytes_text(const uint8_t* b, size_t n, char* out, size_t out_len, const char* empty) {
  if (n == 0) {
    copy_str(out, out_len, empty);
  } else if (!display_safe(b, n, out, out_len)) {
    copy_str(out, out_len, "<non-ASCII>");
  }
}

void hex_text(const uint8_t* b, size_t n, char* out, size_t out_len) {
  static const char kHex[] = "0123456789abcdef";
  size_t j = 0;
  for (size_t i = 0; i < n && j + 2 < out_len; ++i) {
    out[j++] = kHex[b[i] >> 4];
    out[j++] = kHex[b[i] & 0x0F];
  }
  if (out_len > 0) out[j < out_len ? j : out_len - 1] = '\0';
}

const char* auth_short(WifiAuth a) {
  switch (a) {
    case WifiAuth::Open: return "OPEN";
    case WifiAuth::Wep: return "WEP";
    case WifiAuth::WpaPsk: return "WPA";
    case WifiAuth::Wpa2Psk: return "WPA2";
    case WifiAuth::WpaWpa2Psk: return "W1/2";
    case WifiAuth::Wpa2Enterprise: return "ENT";
    case WifiAuth::Wpa3Psk: return "WPA3";
    case WifiAuth::Wpa2Wpa3Psk: return "W2/3";
    case WifiAuth::WapiPsk: return "WAPI";
    case WifiAuth::Unknown:
    default: return "?";
  }
}

const char* policy_short(lab::Policy p) {
  switch (p) {
    case lab::Policy::Public: return "pub";
    case lab::Policy::LinkEncryption: return "enc";
    case lab::Policy::LinkAuthentication: return "mitm";
    case lab::Policy::AppAuthorization: return "app";
    case lab::Policy::None:
    default: return "-";
  }
}

const char* seen_short(Seen s) {
  switch (s) {
    case Seen::NoScan: return "no scan";
    case Seen::NotSeen: return "not seen";
    case Seen::ByAddress: return "addr?";
    case Seen::ByUuid: return "uuid?";
    case Seen::ByName: return "name?";
  }
  return "?";
}

const char* seen_long(Seen s) {
  switch (s) {
    case Seen::NoScan: return "no BLE discovery yet";
    case Seen::NotSeen: return "not seen in last discovery";
    case Seen::ByAddress: return "address seen in last discovery";
    case Seen::ByUuid: return "service UUID seen in last discovery";
    case Seen::ByName: return "name seen in last discovery";
  }
  return "?";
}

const EnrolledTarget* assess_target() {
  if (s_target_count == 0) return nullptr;
  if (s_assess_target >= s_target_count) s_assess_target = 0;
  return &s_targets[s_assess_target];
}

bool target_allows(const EnrolledTarget& t, Operation op) { return (t.allowed_ops_mask & op_bit(op)) != 0; }

// Job operations this firmware executes (mirrors implemented() in assessment_runner.cpp); the others are
// answered as unsupported (M4).
bool operation_implemented_here(Operation op) {
  return op == Operation::WifiSurvey || op == Operation::BleDiscover || op == Operation::BleInspectGatt ||
         op == Operation::BleReadProfileValue;
}

// ---- navigation ---------------------------------------------------------------------------------

void on_enter(Screen s, bool returning);

void nav_go(Screen s) {
  if (s == s_cur) {
    mark_all();
    return;
  }
  constexpr uint8_t cap = sizeof(s_stack) / sizeof(s_stack[0]);
  if (s_depth >= cap) {
    std::memmove(s_stack, s_stack + 1, sizeof(s_stack[0]) * (cap - 1));
    s_depth = cap - 1;
  }
  s_stack[s_depth++] = s_cur;
  s_cur = s;
  on_enter(s, false);
  mark_all();
}

void nav_back() {
  Screen prev = Screen::MainMenu;
  while (s_depth > 0) {
    prev = s_stack[--s_depth];
    if (prev != Screen::Boot && prev != Screen::Provisioning) break;
    prev = Screen::MainMenu;
  }
  if (s_cur == Screen::MainMenu && prev == Screen::MainMenu) return;
  s_cur = prev;
  on_enter(prev, true);
  mark_all();
}

// HOME: clears the stack to the OysterOS root. A confirm dialog is answered by the caller first.
void nav_home() {
  s_depth = 0;
  if (s_cur != Screen::MainMenu) {
    s_cur = Screen::MainMenu;
    on_enter(Screen::MainMenu, true);
  }
  mark_all();
}

void show_detail(Screen s, const char* title, void (*builder)(char*, size_t), const char* static_body) {
  copy_str(s_detail_title, sizeof(s_detail_title), title);
  s_detail_builder = builder;
  if (builder != nullptr) {
    builder(s_detail_body, sizeof(s_detail_body));
  } else {
    copy_str(s_detail_body, sizeof(s_detail_body), static_body);
  }
  if (s_cur == s) {
    s_scroll = 0;
    mark_all();
  } else {
    nav_go(s);
  }
}

void show_message(const char* title, const char* body) { show_detail(Screen::Message, title, nullptr, body); }

void list_move(ListNav& n, uint16_t count, NavEvent e) {
  if (count == 0) {
    n.sel = n.top = 0;
    return;
  }
  switch (e) {
    case NavEvent::Up: n.sel = n.sel > 0 ? n.sel - 1 : count - 1; break;
    case NavEvent::Down: n.sel = (n.sel + 1 < count) ? n.sel + 1 : 0; break;
    case NavEvent::Left: n.sel = n.sel >= kListRows ? n.sel - kListRows : 0; break;
    case NavEvent::Right: n.sel = (n.sel + kListRows < count) ? n.sel + kListRows : count - 1; break;
    default: return;
  }
  if (n.sel < n.top) n.top = n.sel;
  if (n.sel >= n.top + kListRows) n.top = n.sel - kListRows + 1;
  s_dirty_content = true;
}

// Keeps a selection valid after the list size changed. True when `top` moved.
bool clamp_nav(ListNav& n, uint16_t count) {
  const uint16_t old_top = n.top;
  if (count == 0) {
    n.sel = n.top = 0;
  } else {
    if (n.sel >= count) n.sel = count - 1;
    if (n.sel < n.top) n.top = n.sel;
    if (n.sel >= n.top + kListRows) n.top = n.sel - kListRows + 1;
    if (n.top + kListRows > count) n.top = count > kListRows ? count - kListRows : 0;
  }
  return n.top != old_top;
}

// Tile grids (Home: 2 columns, Security: 1 column): `sel` is the row-major tile index, `top` the first
// visible tile row. Up/Down move by row with wrap-around, Left/Right switch column.
void tile_scroll(ListNav& n, uint8_t cols) {
  const uint16_t row = n.sel / cols;
  if (row < n.top) n.top = row;
  if (row >= n.top + ui::kTileRows) n.top = static_cast<uint16_t>(row - ui::kTileRows + 1);
}

void tile_move(ListNav& n, uint8_t count, uint8_t cols, NavEvent e) {
  if (count == 0 || cols == 0) {
    n.sel = n.top = 0;
    return;
  }
  const uint16_t rows = static_cast<uint16_t>((count + cols - 1) / cols);
  uint16_t row = n.sel / cols;
  uint16_t col = n.sel % cols;
  switch (e) {
    case NavEvent::Up: row = row > 0 ? row - 1 : rows - 1; break;
    case NavEvent::Down: row = (row + 1 < rows) ? row + 1 : 0; break;
    case NavEvent::Left:
    case NavEvent::Right:
      if (cols < 2) return;
      col = static_cast<uint16_t>((col + (e == NavEvent::Right ? 1 : cols - 1)) % cols);
      break;
    default: return;
  }
  uint16_t sel = static_cast<uint16_t>(row * cols + col);
  if (sel >= count) sel = count - 1;
  n.sel = sel;
  tile_scroll(n, cols);
  s_dirty_content = true;
}

// ---- configuration, targets, session -------------------------------------------------------------

void load_config() {
  s_cfg = DeviceConfig{};
  s_cfg_ok = g_config.load(s_cfg);
  if (s_cfg_ok && is_valid_device_id(s_cfg.device_id)) {
    copy_str(s_device_id, sizeof(s_device_id), s_cfg.device_id);
    s_device_id_fallback = false;
  } else {
    // Not provisioned yet: a local-only id derived from the Wi-Fi MAC (no identity claim to any backend).
    char hex[13];
    hex_text(g_board.info().mac, 6, hex, sizeof(hex));
    std::snprintf(s_device_id, sizeof(s_device_id), "unprov-%s", hex);
    s_device_id_fallback = true;
  }
  s_target_count = static_cast<uint8_t>(g_config.load_targets(s_targets, cfg::kMaxEnrolledTargets));
  if (s_target_count > cfg::kMaxEnrolledTargets) s_target_count = cfg::kMaxEnrolledTargets;
  if (s_assess_target >= s_target_count) s_assess_target = 0;
  s_seen_version = 0xFFFFFFFFu;
}

// Re-reads the stored setup into the UI state and has the runner reload its target list.
void reload_config_state() {
  load_config();
  s_cfg_gen = g_provisioning.config_generation();
  if (s_runtime) g_runner.reload_targets();
  mark_all();
}

bool open_session_now() {
  char id[cfg::kIdBufLen] = {};
  s_session_ok = g_evidence.new_session_id(id) && g_evidence.open_session(id, s_device_id);
  if (s_session_ok) {
    copy_str(s_session, sizeof(s_session), id);
    if (s_runtime) g_runner.set_session(s_session);
  } else {
    s_session[0] = '\0';
  }
  s_dirty_status = true;
  return s_session_ok;
}

// Last-discovery sightings of enrolled targets. A match is a hint only, never an identity claim.
void compute_sightings(bool force) {
  if (!s_runtime) {
    for (uint8_t i = 0; i < s_target_count; ++i) s_seen[i] = Seen::NoScan;
    return;
  }
  const uint32_t v = g_results.version(ResultKind::Ble);
  if (!force && v == s_seen_version) return;
  s_seen_version = v;
  for (uint8_t i = 0; i < s_target_count; ++i) s_seen[i] = (v == 0) ? Seen::NoScan : Seen::NotSeen;
  if (v == 0) return;
  constexpr size_t kPage = sizeof(s_ble_page) / sizeof(s_ble_page[0]);
  BleSummary sum{};
  size_t start = 0;
  for (;;) {
    const size_t n = g_results.ble_rows(start, s_ble_page, kPage, sum);
    for (size_t j = 0; j < n; ++j) {
      const BleAdvObservation& o = s_ble_page[j];
      for (uint8_t t = 0; t < s_target_count; ++t) {
        const EnrolledTarget& tg = s_targets[t];
        Seen hit = Seen::NotSeen;
        if (tg.has_address && std::memcmp(o.addr, tg.address, 6) == 0) hit = Seen::ByAddress;
        if (hit == Seen::NotSeen && tg.has_service_uuid) {
          for (uint8_t u = 0; u < o.svc_uuid_count && u < cfg::kMaxSvcUuidsPerAdv; ++u) {
            if (std::memcmp(o.svc_uuid128[u], tg.service_uuid128, 16) == 0) hit = Seen::ByUuid;
          }
        }
        const size_t nl = std::strlen(tg.advertised_name);
        if (hit == Seen::NotSeen && nl > 0 && nl == o.name_len && std::memcmp(o.name, tg.advertised_name, nl) == 0) {
          hit = Seen::ByName;
        }
        if (hit != Seen::NotSeen && (s_seen[t] == Seen::NotSeen || hit < s_seen[t])) s_seen[t] = hit;
      }
    }
    if (n < kPage) break;
    start += n;
    if (start >= cfg::kMaxBleAdverts) break;
  }
  s_ble_n = 0;  // the list page buffer was reused
}

void refresh_counts() {
  if (!s_runtime) return;
  ApObservation ap;
  WifiSummary ws{};
  g_results.wifi_rows(0, &ap, 1, ws);
  s_count_wifi = ws.count;
  BleSummary bs{};
  g_results.ble_rows(0, s_ble_page, 1, bs);
  s_count_ble = bs.count;
  s_ble_n = 0;
  GattEntry ge;
  GattSummary gs{};
  g_results.gatt_rows(0, &ge, 1, gs);
  s_count_gatt = gs.count;
}

void build_assess_items() {
  s_acount = 0;
  s_aitems[s_acount++] = {AItem::Target, 0};
  s_aitems[s_acount++] = {AItem::Inspect, 0};
  for (uint8_t i = 0; i < lab::kCharCount; ++i) {
    if (lab::kChars[i].read != lab::Policy::None) s_aitems[s_acount++] = {AItem::Read, i};
  }
  s_aitems[s_acount++] = {AItem::GattResults, 0};
  s_aitems[s_acount++] = {AItem::LastRead, 0};
  for (uint8_t i = 0; i < 4; ++i) s_aitems[s_acount++] = {AItem::NotInBuild, i};
}

// ---- startup failures -------------------------------------------------------------------------------

struct StartupModule {
  uint8_t bit;
  const char* name;
  const char* effect;
};

const StartupModule kStartupModules[] = {
    {kStartConfig, "config", "NVS settings unreadable: no backend, no enrolled targets."},
    {kStartEvidence, "evidence", "Results cannot be recorded: every operation is disabled."},
    {kStartBle, "BLE", "BLE discovery, GATT inspect and reads are disabled. Wi-Fi survey still works."},
    {kStartBackend, "backend", "Ask Oyster, sync and backend jobs are disabled."},
    {kStartRunner, "runner", "The worker task is not running: every operation and backend job is disabled."},
    {kStartUiQueue, "ui-queue", "Operation progress cannot reach the UI: operations are disabled."},
    {kStartSession, "session", "No evidence session is open: operations are disabled. Settings > New session retries."},
};

// "config,BLE,runner" for the modules that failed to start.
void startup_names(char* out, size_t n) {
  Body b = body_of(out, n);
  bool first = true;
  for (const StartupModule& m : kStartupModules) {
    if ((s_start_fail & m.bit) == 0) continue;
    b.add("%s%s", first ? "" : ",", m.name);
    first = false;
  }
}

// Why `kind` must not be queued because a module it depends on did not start; nullptr when nothing blocks it.
const char* startup_block(RequestKind kind) {
  if (s_start_fail == 0) return nullptr;
  const bool ble_kind = kind == RequestKind::BleDiscover || kind == RequestKind::InspectGatt ||
                        kind == RequestKind::ReadValue;
  const bool backend_kind = kind == RequestKind::AskOyster || kind == RequestKind::CancelRun ||
                            kind == RequestKind::SyncNow;
  if (s_start_fail & kStartRunner) {
    return "The worker task did not start (runner), so nothing can run. Reboot to retry. See Settings > Diagnostics.";
  }
  if (s_start_fail & kStartUiQueue) {
    return "The UI event queue was not created, so operation progress cannot be shown. Reboot to retry.";
  }
  if ((s_start_fail & kStartEvidence) && kind != RequestKind::CancelRun) {
    return "The evidence store did not start, so results could not be recorded. Reboot to retry.";
  }
  if ((s_start_fail & kStartBle) && ble_kind) {
    return "The BLE stack did not start. BLE operations are disabled; Wi-Fi survey still works. Reboot to retry.";
  }
  if ((s_start_fail & (kStartBackend | kStartConfig)) && backend_kind) {
    return "The backend client or its settings did not start. Ask Oyster and sync are disabled. Reboot to retry.";
  }
  return nullptr;
}

void build_startup_failure(char* out, size_t n) {
  Body b = body_of(out, n);
  b.add("These modules did not start:\n");
  for (const StartupModule& m : kStartupModules) {
    if (s_start_fail & m.bit) b.add("- %s: %s\n", m.name, m.effect);
  }
  b.add("Reboot to retry. Settings > Diagnostics lists the startup state.");
}

// ---- work requests --------------------------------------------------------------------------------

bool submit_request(const WorkRequest& r, const char* label, bool allow_when_busy = false) {
  if (!s_runtime) {
    show_message("!Unavailable", "Radio, backend and runner modules are not running on this board.");
    return false;
  }
  if (const char* why = startup_block(r.kind)) {
    show_message("!Startup failure", why);
    return false;
  }
  if (!s_session_ok && r.kind != RequestKind::CancelRun) {
    show_message("!No session",
                 "No evidence session is open, so results could not be recorded. "
                 "Check the SD card, then use Settings > New session.");
    return false;
  }
  if (!allow_when_busy && op_in_flight()) {
    toast("Busy - wait or Fn+` to cancel");
    return false;
  }
  if (!g_runner.enqueue(r)) {
    toast("Runner queue full - try again");
    return false;
  }
  if (!allow_when_busy) {
    s_op.requested = true;
    s_op.requested_ms = millis();
    s_op.items = 0;
    copy_str(s_op.label, sizeof(s_op.label), label);
  } else {
    toast("Sent: %s", label);
  }
  mark_all();
  return true;
}

WorkRequest make_request(RequestKind k) {
  WorkRequest r;
  std::memset(&r, 0, sizeof(r));
  r.kind = k;
  return r;
}

void emergency_cancel() {
  if (s_cur == Screen::BoardError) return;
  if (s_runtime) g_runner.cancel_current();
  s_cancel_ack_until = millis() + kCancelAckMs;
  if (s_cur == Screen::TextEntry) nav_back();
  s_dirty_footer = true;
  s_dirty_content = true;
}

// ---- local confirmation ---------------------------------------------------------------------------

void resolve_pending(bool approve) {
  LocalConfirmation c;
  std::memset(&c, 0, sizeof(c));
  copy_str(c.job_id, sizeof(c.job_id), s_pc.job_id);
  copy_str(c.nonce, sizeof(c.nonce), s_pc.nonce);
  c.approved = approve;
  c.decided_ms = static_cast<uint64_t>(esp_timer_get_time() / 1000);
  if (s_runtime) g_runner.resolve_confirmation(c);
  copy_str(s_resolved_job, sizeof(s_resolved_job), s_pc.job_id);
  copy_str(s_resolved_nonce, sizeof(s_resolved_nonce), s_pc.nonce);
  s_pc_shown = false;
  if (s_cur == Screen::ConfirmDialog) nav_back();
  toast("%s: %s", approve ? "Approved" : "Rejected", s_pc.operation);
}

void poll_confirmation(uint32_t now) {
  if (!s_runtime) return;
  PendingConfirmation pc;
  std::memset(&pc, 0, sizeof(pc));
  const bool active = g_runner.pending_confirmation(pc) && pc.active;
  if (active) {
    if (std::strcmp(pc.job_id, s_resolved_job) == 0 && std::strcmp(pc.nonce, s_resolved_nonce) == 0) return;
    const bool same = s_pc_shown && std::strcmp(pc.job_id, s_pc.job_id) == 0 && std::strcmp(pc.nonce, s_pc.nonce) == 0;
    s_pc = pc;
    s_pc_fetched_ms = now;
    if (!same) {
      s_pc_shown = true;
      s_pc_guard_until = now + kConfirmGuardMs;
      if (s_cur == Screen::ConfirmDialog) {
        mark_all();
      } else {
        nav_go(Screen::ConfirmDialog);
      }
    }
    const uint32_t secs = pc.remaining_ms / 1000;
    if (secs != s_pc_last_secs) {
      s_pc_last_secs = secs;
      s_dirty_footer = true;
    }
  } else if (s_pc_shown) {
    s_pc_shown = false;
    if (s_cur == Screen::ConfirmDialog) nav_back();
    toast("Confirmation closed (expired?)");
  }
}

// ---- body builders (detail / live views) -----------------------------------------------------------

void build_board_error(char* out, size_t n) {
  Body b = body_of(out, n);
  const BoardInfo& bi = g_board.info();
  b.add("This firmware needs an M5Stack Cardputer ADV.\n");
  b.add("Detected board id: %d%s\n", bi.board_id, bi.board_id == 14 ? " (original Cardputer)" : "");
  b.add("Expected: %d (M5CardputerADV).\n", static_cast<int>(m5::board_t::board_M5CardputerADV));
  b.add("The ADV keyboard driver (TCA8418) is only selected on the ADV, so input, radio and "
        "backend are disabled. Flash firmware built for this board.");
}

void build_evidence(char* out, size_t n) {
  Body b = body_of(out, n);
  b.add("Saved findings = the evidence log of this session.\n");
  b.add("Session: %s\n", s_session_ok ? s_session : "none (not open)");
  const char* boot = g_evidence.boot_id();
  b.add("Boot: %s\n", boot && boot[0] ? boot : "unknown");
  b.add("Device: %s%s\n", s_device_id, s_device_id_fallback ? " (unprovisioned)" : "");
  b.add("Storage: %s\n", g_evidence.sd_backed() ? "SD card" : "volatile RAM ring (lost on reset)");
  b.add("SD state: %s\n", to_string(g_board.storage().state));
  const uint32_t next = g_evidence.next_sequence();
  const uint32_t last = next > 0 ? next - 1 : 0;
  const uint32_t resolved = g_evidence.resolved_sequence();  // upload cursor: accepted, skipped or lost
  b.add("Events written (last seq): %lu\n", static_cast<unsigned long>(last));
  // acked_sequence() is the highest sequence the backend ACCEPTED; refused events are never counted in it.
  b.add("Accepted by backend, highest seq: %lu\n", static_cast<unsigned long>(g_evidence.acked_sequence()));
  b.add("Skipped (refused by backend): %lu\n", static_cast<unsigned long>(g_evidence.skipped_events()));
  b.add("Awaiting upload: %lu\n", static_cast<unsigned long>(last > resolved ? last - resolved : 0));
  b.add("Dropped events: %lu\n", static_cast<unsigned long>(g_evidence.dropped_events()));
  b.add("Dropped UI events: %lu\n", static_cast<unsigned long>(s_ui_dropped));
  b.add("Last operation: %s\n", s_last_result);
  b.add("Backend: %s\n", s_runtime ? to_string(g_backend.state()) : "not started");
  b.add("Events of an earlier boot stay on the SD card but are NOT uploaded after a reboot (known gap, "
        "milestone M6). Upload covers this session only.\n");
  b.add("Findings are evaluated on the backend (Ask Oyster or the report).");
}

void build_run(char* out, size_t n) {
  Body b = body_of(out, n);
  if (!ai_available()) {
    b.add("AI unavailable: backend %s.\nNo answer is generated while offline.\n\n",
          s_runtime ? to_string(g_backend.state()) : "not started");
  }
  if (!s_runtime) return;
  if (s_op.requested) b.add("Question sent; waiting for the backend to start the run.\n\n");
  static RunSummary r;  // about 1.5 KB: kept off the UI task stack
  std::memcpy(&r, &g_backend.latest_run(), sizeof(r));  // display copy; may lag the worker by one update
  r.run_id[sizeof(r.run_id) - 1] = '\0';
  r.status[sizeof(r.status) - 1] = '\0';
  r.answer_excerpt[sizeof(r.answer_excerpt) - 1] = '\0';
  r.pending_operation[sizeof(r.pending_operation) - 1] = '\0';
  r.pending_target[sizeof(r.pending_target) - 1] = '\0';
  r.pending_effect[sizeof(r.pending_effect) - 1] = '\0';
  if (r.run_id[0] == '\0') {
    if (!s_op.requested) b.add("No AI run yet in this boot.");
    return;
  }
  b.add("Run: %s\n", r.run_id);
  b.add("Status: %s\n", r.status[0] ? r.status : "unknown");
  if (std::strcmp(r.status, "ai_unavailable") == 0) {
    b.add("The backend could not use an AI provider for this run. No answer was generated.\n");
  }
  if (r.answer_excerpt[0]) b.add("\n%s\n", r.answer_excerpt);
  if (r.has_pending_action) {
    b.add("\nPending action: %s\nTarget: %s\n%s\n", r.pending_operation,
          r.pending_target[0] ? r.pending_target : "(none)", r.pending_effect);
    // What the device will do depends on the operation type, not on the run.
    Operation pop = Operation::Unknown;
    if (!parse_operation(r.pending_operation, pop)) {
      b.add("Unknown operation: the device refuses it.");
    } else if (operation_requires_local_confirmation(pop)) {
      b.add("This operation needs local confirmation, but it is not implemented in this build: the device "
            "answers the job as unsupported.");
    } else if (!operation_implemented_here(pop)) {
      b.add("Not implemented in this build (M4): the device answers the job as unsupported.");
    } else {
      b.add("No local confirmation is needed. It runs when the backend sends the job, if the target is "
            "enrolled and in scope.");
    }
  }
}

void build_backend(char* out, size_t n) {
  Body b = body_of(out, n);
  b.add("State: %s\n", s_runtime ? to_string(g_backend.state()) : "not started");
  if (!s_cfg_ok) {
    b.add("Config: not readable\n");
  } else {
    b.add("Host: %s:%u\n", s_cfg.backend_host[0] ? s_cfg.backend_host : "(not set)",
          static_cast<unsigned>(s_cfg.backend_port));
    char ssid[sizeof(s_cfg.wifi_ssid)];
    const size_t sl = strnlen(s_cfg.wifi_ssid, sizeof(s_cfg.wifi_ssid) - 1);
    bytes_text(reinterpret_cast<const uint8_t*>(s_cfg.wifi_ssid), sl, ssid, sizeof(ssid), "(not set)");
    b.add("Wi-Fi SSID: %s\n", ssid);
  }
  b.add("Device id: %s%s\n", s_device_id, s_device_id_fallback ? " (local fallback)" : "");
  b.add("Provisioned: %s\n", g_config.is_provisioned() ? "yes" : "no");
  b.add("CA certificate: %u bytes\n", static_cast<unsigned>(g_config.ca_pem_len()));
  b.add("Enrolled targets: %u\n", static_cast<unsigned>(s_target_count));
  b.add("Token and Wi-Fi password are never shown. Provision over USB: scripts/provision.ps1.");
}

void build_storage(char* out, size_t n) {
  Body b = body_of(out, n);
  const StorageInfo st = g_board.storage();
  b.add("SD state: %s\n", to_string(st.state));
  b.add("Card type: %u\n", static_cast<unsigned>(st.card_type));
  b.add("Total: %lu MB\n", static_cast<unsigned long>(st.total_bytes / (1024ull * 1024ull)));
  b.add("Used: %lu MB\n", static_cast<unsigned long>(st.used_bytes / (1024ull * 1024ull)));
  b.add("Evidence: %s\n", g_evidence.sd_backed() ? "SD card" : "volatile RAM ring (bounded)");
  b.add("A card inserted after boot is used after a reboot.");
}

void build_diag(char* out, size_t n) {
  Body b = body_of(out, n);
  TelemetrySnapshot t;
  telemetry_snapshot(t);
  if (s_start_fail == 0) {
    b.add(s_runtime ? "Startup: all modules started\n" : "Startup: radio, BLE, backend, runner off (wrong board)\n");
  } else {
    char names[64];
    startup_names(names, sizeof(names));
    b.add("Startup FAILED: %s\n", names);
    for (const StartupModule& m : kStartupModules) {
      if (s_start_fail & m.bit) b.add(" %s: %s\n", m.name, m.effect);
    }
  }
  b.add("Evidence store: %s\n", g_evidence.sd_backed() ? "SD card" : "volatile RAM (no SD)");
  b.add("Uptime: %lu s\n", static_cast<unsigned long>(t.uptime_ms / 1000));
  b.add("Heap free: %lu\n", static_cast<unsigned long>(t.free_heap));
  b.add("Heap min: %lu (%s)\n", static_cast<unsigned long>(t.min_free_heap), t.heap_goal_met ? "goal met" : "BELOW goal");
  b.add("Largest blk: %lu (%s)\n", static_cast<unsigned long>(t.largest_free_block),
        t.largest_block_goal_met ? "goal met" : "BELOW goal");
  b.add("Reset: %s (%d)\n", t.reset_reason_str ? t.reset_reason_str : "unknown", t.reset_reason);
  b.add("Stack HWM bytes:\n");
  for (uint8_t i = 0; i < t.task_count && i < 8; ++i) {
    b.add(" %s %lu\n", t.tasks[i].name ? t.tasks[i].name : "?", static_cast<unsigned long>(t.tasks[i].stack_hwm_bytes));
  }
  b.add("Queues (wait/depth drop):\n");
  for (uint8_t i = 0; i < t.queue_count && i < 8; ++i) {
    b.add(" %s %u/%u %lu\n", t.queues[i].name ? t.queues[i].name : "?", static_cast<unsigned>(t.queues[i].waiting),
          static_cast<unsigned>(t.queues[i].depth), static_cast<unsigned long>(t.queues[i].dropped));
  }
  b.add("Radio: %s\n", s_runtime ? to_string(g_radio.active()) : "not started");
  const int batt = g_board.battery_percent();
  if (batt >= 0) {
    b.add("Battery: %d%%", batt);
  } else {
    b.add("Battery: unknown");
  }
}

void build_about(char* out, size_t n) {
  Body b = body_of(out, n);
  const BoardInfo& bi = g_board.info();
  char mac[18];
  format_mac(bi.mac, mac, sizeof(mac));
  b.add("Oyster Pocket firmware %s\n", OYSTER_FIRMWARE_VERSION);
  b.add("Board id: %d (%s)\n", bi.board_id, bi.is_cardputer_adv ? "Cardputer ADV" : "MISMATCH");
  b.add("Flash: %lu MB, PSRAM: %s\n", static_cast<unsigned long>(bi.flash_bytes / (1024ul * 1024ul)),
        bi.psram_found ? "found" : "none");
  b.add("Wi-Fi MAC: %s\n", mac);
  b.add("Reset reason: %s\n", bi.reset_reason_str ? bi.reset_reason_str : "unknown");
  b.add("Keys: see Settings > Keyboard (proposed mapping, not yet confirmed on hardware).");
}

// Settings > Keyboard and the Help screen: the proposed bindings (input_map.h), never claimed as verified.
void build_keys(Body& b) {
  b.add("Proposed keys, NOT yet confirmed on hardware:\n");
  b.add("Up/Down: ; .  (Fn+; . anywhere)\n");
  b.add("Left/Right: , /  (page, adjust)\n");
  b.add("Select: Enter\n");
  b.add("Back: ` or Del. A dialog is answered 'reject' first.\n");
  b.add("Home: Fn+Del (press Fn first). Back to the root.\n");
  b.add("Help: ? (Shift+/). Not in text entry.\n");
  b.add("Cancel a task: Fn+` or Ctrl+C, on every screen.\n");
  b.add("Text entry: Del erases, Del on empty leaves, Fn+, / move the caret.\n");
  b.add("Home screen: 1-8 open a tile.\n");
  b.add("Settings > Key tester shows what each key does. Record results in docs/HARDWARE_VERIFICATION.md.");
}

void build_keyboard(char* out, size_t n) {
  Body b = body_of(out, n);
  build_keys(b);
}

// Design screen id and a one-line hint for the Help screen.
const char* screen_hint(Screen s, const char** name) {
  switch (s) {
    case Screen::MainMenu: *name = "Home [H01]"; return "Arrows move between tiles, Enter opens, 1-8 open directly.";
    case Screen::Security: *name = "Security [S10]"; return "Pick a tool. Enter opens it.";
    case Screen::WifiSurvey:
      *name = "Wi-Fi Survey [S11]";
      return "Start a passive 2.4 GHz survey, then open the results. The room label is stored with it.";
    case Screen::WifiResults: *name = "Wi-Fi results [S12]"; return "Enter: detail. s: sort by RSSI or channel. , /: page.";
    case Screen::BleExplorer:
      *name = "Bluetooth Explorer [S21]";
      return "Passive discovery only listens; active discovery sends scan requests.";
    case Screen::BleResults: *name = "BLE results [S22]"; return "Enter: detail. , /: page.";
    case Screen::Detail: *name = "Detail [S13/S23]"; return "; . scroll, , / page. Back returns to the list.";
    case Screen::Evidence: *name = "Saved Findings"; return "Upload and session state of the evidence log. ; . scroll.";
    case Screen::Targets: *name = "Enrolled Targets"; return "Enter: detail. a: assess the selected target.";
    case Screen::Assess: *name = "Assess"; return "Enter runs the selected check on the enrolled target. , /: target.";
    case Screen::GattTree: *name = "GATT results"; return "Enter: detail of a service or characteristic.";
    case Screen::AskOyster: *name = "Oyster AI [A10]"; return "Type or speak (v) a question, read the latest run, cancel it or sync.";
    case Screen::RunView: *name = "Oyster AI run [A11/A12]"; return "Status and answer pages. ; . scroll.";
    case Screen::Settings: *name = "Settings [P10]"; return "Enter opens an entry. , / adjust the brightness.";
    case Screen::Unavailable: *name = "Unavailable module"; return "This module is not in this build. Back returns.";
    case Screen::Module: *name = modules::name(s_module); return "Enter opens the selected row. Back goes up one level. r refreshes where offered.";
    case Screen::Message: *name = "Message"; return "; . scroll. Back returns.";
    case Screen::LedLegend: *name = "LED colours"; return "What the status LED shows. ; . move. Back returns.";
    default: *name = "This screen"; return "Back returns.";
  }
}

void build_help(char* out, size_t n) {
  Body b = body_of(out, n);
  const char* name = nullptr;
  const char* hint = screen_hint(s_help_from, &name);
  b.add("%s: %s\nEnter: what the LED colours mean.\n\n", name, hint);
  build_keys(b);
}

void build_keytest(char* out, size_t n) {
  Body b = body_of(out, n);
  b.add("Keys: %s\n", s_kt.keys[0] ? s_kt.keys : "(press a key)");
  b.add("Action: %s\n", s_kt.action[0] ? s_kt.action : "-");
  b.add("Before: %s\n", s_kt.before[0] ? s_kt.before : "-");
  b.add("Changes: %u. Leave: Back or Home twice.\n", static_cast<unsigned>(s_kt.presses));
  b.add("Navigation mode. Cancel (Fn+` / Ctrl+C) still cancels.");
}

void build_read(char* out, size_t n) {
  Body b = body_of(out, n);
  ReadResultView r{};
  if (!s_runtime || !g_results.latest_read(r)) {
    b.add("No read result yet. Use Assess > Read <characteristic>.");
    return;
  }
  r.target_id[sizeof(r.target_id) - 1] = '\0';
  r.characteristic_id[sizeof(r.characteristic_id) - 1] = '\0';
  r.event_id[sizeof(r.event_id) - 1] = '\0';
  b.add("Target: %s\n", r.target_id);
  b.add("Characteristic: %s\n", r.characteristic_id);
  b.add("Status: %s\n", status_name(static_cast<uint32_t>(r.status)));
  b.add("Length: %u bytes\n", static_cast<unsigned>(r.value_length));
  if (r.preview_withheld) {
    b.add("Value withheld (non-public policy): only length and SHA-256 are recorded.\n");
  } else if (r.preview_len > 0) {
    char hex[2 * sizeof(r.preview) + 1];
    hex_text(r.preview, r.preview_len < sizeof(r.preview) ? r.preview_len : sizeof(r.preview), hex, sizeof(hex));
    b.add("Value (hex): %s%s\n", hex, r.value_length > r.preview_len ? " ..." : "");
  }
  b.add("Encrypted: %s\n", yes_no_unknown(r.sec.encrypted));
  b.add("Authenticated pairing: %s\n", yes_no_unknown(r.sec.authenticated_pairing));
  b.add("App authorized: %s\n", yes_no_unknown(r.sec.application_authorized));
  b.add("Event: %s", r.event_id[0] ? r.event_id : "(none)");
}

// ---- list row text ----------------------------------------------------------------------------------

void wifi_menu_row(uint16_t i, char* out, size_t n) {
  switch (i) {
    case 0: std::snprintf(out, n, "Start survey (passive)"); break;
    case 1: std::snprintf(out, n, "Results (%u)", static_cast<unsigned>(s_count_wifi)); break;
    case 2: std::snprintf(out, n, "Room: %s", s_room[0] ? s_room : "(none)"); break;
    default: out[0] = '\0'; break;
  }
}

void ble_menu_row(uint16_t i, char* out, size_t n) {
  switch (i) {
    case 0: std::snprintf(out, n, "Discover (passive)"); break;
    case 1: std::snprintf(out, n, "Discover (active)"); break;
    case 2: std::snprintf(out, n, "Results (%u)", static_cast<unsigned>(s_count_ble)); break;
    default: out[0] = '\0'; break;
  }
}

// Unavailable entries are shown muted (design: disabled state, never a dead control).
bool ble_menu_disabled(uint16_t i) { return i < 2 && (s_start_fail & kStartBle) != 0; }
bool assess_disabled(uint16_t i) { return i < s_acount && s_aitems[i].kind == AItem::NotInBuild; }

void target_row(uint16_t i, char* out, size_t n) {
  if (i >= s_target_count) {
    out[0] = '\0';
    return;
  }
  std::snprintf(out, n, "%-19.19s %s", strip_prefix(s_targets[i].target_id, "target-"), seen_short(s_seen[i]));
}

void assess_row(uint16_t i, char* out, size_t n) {
  if (i >= s_acount) {
    out[0] = '\0';
    return;
  }
  const AssessItem& it = s_aitems[i];
  const EnrolledTarget* t = assess_target();
  switch (it.kind) {
    case AItem::Target:
      if (t == nullptr) {
        std::snprintf(out, n, "Target: (none enrolled)");
      } else {
        std::snprintf(out, n, "Target%s %s", s_target_count > 1 ? " <>" : ":", strip_prefix(t->target_id, "target-"));
      }
      break;
    case AItem::Inspect: std::snprintf(out, n, "Inspect GATT"); break;
    case AItem::Read:
      std::snprintf(out, n, "Read %s [%s]", lab::kChars[it.idx].id, policy_short(lab::kChars[it.idx].read));
      break;
    case AItem::GattResults: std::snprintf(out, n, "GATT results (%u)", static_cast<unsigned>(s_count_gatt)); break;
    case AItem::LastRead: std::snprintf(out, n, "Last read result"); break;
    case AItem::NotInBuild: std::snprintf(out, n, "%s: not in build", kM4Names[it.idx & 3]); break;
  }
}

void ask_row(uint16_t i, char* out, size_t n) {
  switch (i) {
    case 0: std::snprintf(out, n, "Ask a question"); break;
    case 1: std::snprintf(out, n, "Speak a question (v)"); break;
    case 2: std::snprintf(out, n, "Latest AI run"); break;
    case 3: std::snprintf(out, n, "Cancel AI run"); break;
    case 4: std::snprintf(out, n, "Sync evidence now"); break;
    default: out[0] = '\0'; break;
  }
}

void settings_row(uint16_t i, char* out, size_t n) {
  switch (i) {
    case 0: {
      char st[16];
      modules::tile_status(modules::Id::WifiSetup, st, sizeof(st));
      std::snprintf(out, n, "Wi-Fi: %s", st[0] ? st : "setup");
      break;
    }
    case 1: std::snprintf(out, n, "Backend: %s", backend_short()); break;
    case 2: std::snprintf(out, n, "Brightness: %u/%u", static_cast<unsigned>(s_brightness_idx) + 1, kBrightnessLevels); break;
    case 3: std::snprintf(out, n, "Storage: %s", to_string(g_board.storage().state)); break;
    case 4: std::snprintf(out, n, "Diagnostics"); break;
    case 5: std::snprintf(out, n, "Sync evidence now"); break;
    case 6: std::snprintf(out, n, "New session"); break;
    case 7: std::snprintf(out, n, "Keyboard"); break;
    case 8: std::snprintf(out, n, "Key tester"); break;
    case 9: std::snprintf(out, n, "About"); break;
    default: out[0] = '\0'; break;
  }
}

void wifi_row(uint16_t i, char* out, size_t n) {
  if (i < s_page_start || i - s_page_start >= s_wifi_n) {
    std::snprintf(out, n, "...");
    return;
  }
  const ApObservation& ap = s_wifi_page[i - s_page_start];
  char ssid[cfg::kMaxSsidBytes + 1];
  bytes_text(ap.ssid, ap.ssid_len < cfg::kMaxSsidBytes ? ap.ssid_len : cfg::kMaxSsidBytes, ssid, sizeof(ssid), "<hidden>");
  std::snprintf(out, n, "%4d %2u %-4s %s", static_cast<int>(ap.rssi_dbm), static_cast<unsigned>(ap.channel),
                auth_short(ap.auth), ssid);
}

void ble_row(uint16_t i, char* out, size_t n) {
  if (i < s_page_start || i - s_page_start >= s_ble_n) {
    std::snprintf(out, n, "...");
    return;
  }
  const BleAdvObservation& o = s_ble_page[i - s_page_start];
  char label[cfg::kMaxBleNameBytes + 1];
  const size_t nl = o.name_len < cfg::kMaxBleNameBytes ? o.name_len : cfg::kMaxBleNameBytes;
  if (nl == 0 || !display_safe(o.name, nl, label, sizeof(label))) format_mac(o.addr, label, sizeof(label));
  const char conn = o.connectable.is_true() ? 'C' : (o.connectable.is_false() ? '-' : '?');
  std::snprintf(out, n, "%4d %c %s", static_cast<int>(o.rssi_dbm), conn, label);
}

int lab_char_for(const uint8_t uuid[16]) {
  for (uint8_t i = 0; i < lab::kCharCount; ++i) {
    if (s_lab_char_ok[i] && std::memcmp(uuid, s_lab_char_uuid[i], 16) == 0) return i;
  }
  return -1;
}

void uuid_short(const GattEntry& e, char* out, size_t n) {
  if (e.uuid_bits == 16) {
    hex_text(e.uuid128 + 2, 2, out, n);
  } else {
    hex_text(e.uuid128, 4, out, n);
  }
}

void props_text(uint8_t p, char* out) {
  int j = 0;
  if (p & 0x02) out[j++] = 'R';
  if (p & 0x08) out[j++] = 'W';
  if (p & 0x04) out[j++] = 'w';
  if (p & 0x10) out[j++] = 'N';
  if (p & 0x20) out[j++] = 'I';
  out[j] = '\0';
}

void gatt_row(uint16_t i, char* out, size_t n) {
  if (i < s_page_start || i - s_page_start >= s_gatt_n) {
    std::snprintf(out, n, "...");
    return;
  }
  const GattEntry& e = s_gatt_page[i - s_page_start];
  char u[12];
  uuid_short(e, u, sizeof(u));
  char props[8];
  props_text(e.props, props);
  switch (e.kind) {
    case GattKind::Service:
      if (s_lab_svc_ok && std::memcmp(e.uuid128, s_lab_svc_uuid, 16) == 0) {
        std::snprintf(out, n, "S oyster-lab service");
      } else {
        std::snprintf(out, n, "S %s", u);
      }
      break;
    case GattKind::Characteristic: {
      const int lc = lab_char_for(e.uuid128);
      std::snprintf(out, n, " C %s %s", lc >= 0 ? lab::kChars[lc].id : u, props);
      break;
    }
    case GattKind::Descriptor: std::snprintf(out, n, "   D %s", u); break;
  }
}

// ---- result pages ------------------------------------------------------------------------------------

void load_wifi_page() {
  ListNav& nv = s_nav[sidx(Screen::WifiResults)];
  s_wifi_n = g_results.wifi_rows(nv.top, s_wifi_page, kListRows, s_wifi_sum);
  if (clamp_nav(nv, s_wifi_sum.count)) s_wifi_n = g_results.wifi_rows(nv.top, s_wifi_page, kListRows, s_wifi_sum);
  s_page_start = nv.top;
}

void load_ble_page() {
  ListNav& nv = s_nav[sidx(Screen::BleResults)];
  s_ble_n = g_results.ble_rows(nv.top, s_ble_page, kListRows, s_ble_sum);
  if (clamp_nav(nv, s_ble_sum.count)) s_ble_n = g_results.ble_rows(nv.top, s_ble_page, kListRows, s_ble_sum);
  s_page_start = nv.top;
}

void load_gatt_page() {
  ListNav& nv = s_nav[sidx(Screen::GattTree)];
  s_gatt_n = g_results.gatt_rows(nv.top, s_gatt_page, kListRows, s_gatt_sum);
  if (clamp_nav(nv, s_gatt_sum.count)) s_gatt_n = g_results.gatt_rows(nv.top, s_gatt_page, kListRows, s_gatt_sum);
  s_page_start = nv.top;
}

// ---- detail views of list entries ----------------------------------------------------------------------

void open_ap_detail(uint16_t index) {
  ApObservation ap;
  WifiSummary sum{};
  if (g_results.wifi_rows(index, &ap, 1, sum) != 1) return;
  char text[700];
  Body b = body_of(text, sizeof(text));
  char ssid[cfg::kMaxSsidBytes + 1];
  const size_t sl = ap.ssid_len < cfg::kMaxSsidBytes ? ap.ssid_len : cfg::kMaxSsidBytes;
  bytes_text(ap.ssid, sl, ssid, sizeof(ssid), "<hidden or empty>");
  char hex[2 * cfg::kMaxSsidBytes + 1];
  hex_text(ap.ssid, sl, hex, sizeof(hex));
  char mac[18];
  format_mac(ap.bssid, mac, sizeof(mac));
  sum.room_label[sizeof(sum.room_label) - 1] = '\0';
  b.add("SSID: %s\n", ssid);
  if (sl > 0) b.add("SSID hex: %s\n", hex);
  b.add("BSSID: %s\n", mac);
  b.add("Channel: %u (2.4 GHz)\n", static_cast<unsigned>(ap.channel));
  b.add("RSSI: %d dBm\n", static_cast<int>(ap.rssi_dbm));
  b.add("Auth: %s\n", to_string(ap.auth));
  b.add("WPS/PMF: unknown (not obtained)\n");
  b.add("Room: %s\n", sum.room_label[0] ? sum.room_label : "(none)");
  b.add("Scan: %s, %lu ms%s%s\n", sum.passive ? "passive" : "active", static_cast<unsigned long>(sum.duration_ms),
        sum.complete ? "" : ", incomplete", sum.cancelled ? ", cancelled" : "");
  b.add("Built-in radio: 2.4 GHz only; 5/6 GHz networks are not visible.");
  show_detail(Screen::Detail, "Access point", nullptr, text);
}

void open_ble_detail(uint16_t index) {
  BleAdvObservation o;
  BleSummary sum{};
  if (g_results.ble_rows(index, &o, 1, sum) != 1) return;
  char text[800];
  Body b = body_of(text, sizeof(text));
  char mac[18];
  format_mac(o.addr, mac, sizeof(mac));
  char name[cfg::kMaxBleNameBytes + 1];
  const size_t nl = o.name_len < cfg::kMaxBleNameBytes ? o.name_len : cfg::kMaxBleNameBytes;
  bytes_text(o.name, nl, name, sizeof(name), "(none)");
  b.add("Observation: %u (session-local, not an identity)\n", static_cast<unsigned>(o.obs_id));
  b.add("Address: %s (%s)\n", mac, to_string(o.addr_type));
  b.add("RSSI (last): %d dBm\n", static_cast<int>(o.rssi_dbm));
  b.add("Connectable: %s\n", yes_no_unknown(o.connectable));
  b.add("Name: %s\n", name);
  b.add("Service UUIDs: %u%s\n", static_cast<unsigned>(o.svc_uuid_count), o.svc_uuids_truncated ? " (+more advertised)" : "");
  for (uint8_t u = 0; u < o.svc_uuid_count && u < cfg::kMaxSvcUuidsPerAdv; ++u) {
    char us[37];
    format_uuid128(o.svc_uuid128[u], us, sizeof(us));
    b.add(" %s\n", us);
  }
  b.add("Manufacturer data: %u bytes%s\n", static_cast<unsigned>(o.mfg_len), o.mfg_truncated ? " (truncated)" : "");
  b.add("Seen: %u times\n", static_cast<unsigned>(o.seen_count));
  if (o.malformed) b.add("Payload malformed (kept as observed)\n");
  b.add("Addresses can rotate and names are not unique.");
  show_detail(Screen::Detail, "BLE advertiser", nullptr, text);
}

void open_gatt_detail(uint16_t index) {
  GattEntry e;
  GattSummary sum{};
  if (g_results.gatt_rows(index, &e, 1, sum) != 1) return;
  char text[400];
  Body b = body_of(text, sizeof(text));
  char us[37];
  format_uuid128(e.uuid128, us, sizeof(us));
  const char* kind = e.kind == GattKind::Service ? "service" : (e.kind == GattKind::Characteristic ? "characteristic" : "descriptor");
  b.add("Kind: %s\n", kind);
  b.add("UUID: %s (%u-bit)\n", us, static_cast<unsigned>(e.uuid_bits));
  const int lc = lab_char_for(e.uuid128);
  if (lc >= 0) b.add("Lab profile: %s\n", lab::kChars[lc].id);
  b.add("Handle: 0x%04x\n", static_cast<unsigned>(e.handle));
  if (e.kind == GattKind::Characteristic) {
    char props[8];
    props_text(e.props, props);
    b.add("Properties: 0x%02x %s\n", static_cast<unsigned>(e.props), props);
    b.add("Advertised properties are not a permission guarantee.");
  }
  show_detail(Screen::Detail, "GATT entry", nullptr, text);
}

void build_target_detail(char* out, size_t n) {
  Body b = body_of(out, n);
  const uint16_t i = s_nav[sidx(Screen::Targets)].sel;
  if (i >= s_target_count) {
    b.add("No target.");
    return;
  }
  const EnrolledTarget& t = s_targets[i];
  b.add("Target: %s\n", t.target_id);
  const bool supported = std::strcmp(t.profile_id, lab::kProfileId) == 0;
  b.add("Profile: %s%s\n", t.profile_id, supported ? "" : " (not in this build)");
  b.add("Scope version: %lu\n", static_cast<unsigned long>(t.scope_version));
  b.add("Allowed:");
  for (uint8_t op = 0; op < kJobOperationCount; ++op) {
    if (t.allowed_ops_mask & op_bit(static_cast<Operation>(op))) b.add(" %s", to_string(static_cast<Operation>(op)));
  }
  b.add("\n");
  char name[cfg::kMaxBleNameBytes + 1];
  bytes_text(reinterpret_cast<const uint8_t*>(t.advertised_name), strnlen(t.advertised_name, cfg::kMaxBleNameBytes), name,
             sizeof(name), "(unknown)");
  b.add("Name: %s\n", name);
  if (t.has_service_uuid) {
    char us[37];
    format_uuid128(t.service_uuid128, us, sizeof(us));
    b.add("Service: %s\n", us);
  }
  if (t.has_address) {
    char mac[18];
    format_mac(t.address, mac, sizeof(mac));
    b.add("Address: %s (%s)\n", mac, to_string(t.address_type));
  }
  b.add("Last discovery: %s\n", seen_long(s_seen[i]));
  b.add("A match is a hint only, not an identity check. Enter: assess this target.");
}

// ---- screen actions ------------------------------------------------------------------------------------

char s_entry_prompt[40] = {};
bool s_entry_secret = false;

// Entry page of a module: the module's prompt, pre-filled text, limit and masking; the result goes back to
// modules::entry() and the module decides what to show next.
void open_module_entry(const modules::PageView& v) {
  s_entry_purpose = EntryPurpose::Module;
  copy_str(s_entry_prompt, sizeof(s_entry_prompt), v.entry_prompt[0] ? v.entry_prompt : "Text");
  s_entry_secret = v.entry_secret;
  copy_str(s_entry, sizeof(s_entry), v.entry_initial);
  s_entry_max = v.entry_max < sizeof(s_entry) - 1 ? v.entry_max : sizeof(s_entry) - 1;
  s_entry_len = std::strlen(s_entry);
  s_entry_cursor = s_entry_len;
  nav_go(Screen::TextEntry);
}

void open_entry(EntryPurpose p) {
  s_entry_purpose = p;
  if (p == EntryPurpose::RoomLabel) {
    copy_str(s_entry, sizeof(s_entry), s_room);
    s_entry_max = cfg::kMaxRoomLabelLen;
  } else {
    s_entry[0] = '\0';
    s_entry_max = cfg::kMaxQuestionLen;
  }
  s_entry_len = std::strlen(s_entry);
  s_entry_cursor = s_entry_len;
  nav_go(Screen::TextEntry);
}

bool entry_accepts(char c) {
  if (c < 0x20 || c > 0x7E) return false;
  if (s_entry_purpose == EntryPurpose::RoomLabel) {
    return std::isalnum(static_cast<unsigned char>(c)) || c == ' ' || c == '-' || c == '_' || c == '.' || c == '#';
  }
  return true;
}

void show_ai_unavailable() {
  if (const char* why = startup_block(RequestKind::AskOyster)) {
    show_message("!AI unavailable", why);
    return;
  }
  char text[240];
  std::snprintf(text, sizeof(text),
                "Backend state: %s.\nNo answer is generated offline. Local surveys still work and their "
                "evidence is uploaded when the backend is reachable again (until the next reboot).",
                s_runtime ? to_string(g_backend.state()) : "not started");
  show_message("!AI unavailable", text);
}

void submit_entry() {
  // Trim surrounding spaces.
  size_t start = 0;
  while (start < s_entry_len && s_entry[start] == ' ') ++start;
  size_t end = s_entry_len;
  while (end > start && s_entry[end - 1] == ' ') --end;
  char text[cfg::kMaxQuestionLen + 1];
  const size_t len = end - start;
  std::memcpy(text, s_entry + start, len);
  text[len] = '\0';

  if (s_entry_purpose == EntryPurpose::RoomLabel) {
    copy_str(s_room, sizeof(s_room), text);
    nav_back();
    toast("Room label %s", s_room[0] ? "set" : "cleared");
    return;
  }
  if (s_entry_purpose == EntryPurpose::Module) {
    nav_back();  // back to the module screen; the module changes its page in entry()
    modules::entry(s_module, text);
    volatile char* vz = s_entry;  // may have carried a Wi-Fi password; clear it, not optimised away
    for (size_t z = 0; z < sizeof(s_entry); ++z) vz[z] = 0;
    s_entry_len = s_entry_cursor = 0;
    return;
  }
  if (len == 0) {
    toast("Type a question first");
    return;
  }
  if (!ai_available()) {
    nav_back();
    show_ai_unavailable();
    return;
  }
  WorkRequest r = make_request(RequestKind::AskOyster);
  copy_str(r.question, sizeof(r.question), text);
  if (submit_request(r, "Ask Oyster")) {
    nav_back();
    s_scroll = 0;
    nav_go(Screen::RunView);
  }
}

void wifi_menu_action(uint16_t i) {
  switch (i) {
    case 0: {
      WorkRequest r = make_request(RequestKind::WifiSurvey);
      copy_str(r.room_label, sizeof(r.room_label), s_room);
      r.active_scan = false;
      submit_request(r, "Wi-Fi survey");
      break;
    }
    case 1: nav_go(Screen::WifiResults); break;
    case 2: open_entry(EntryPurpose::RoomLabel); break;
    default: break;
  }
}

void ble_menu_action(uint16_t i) {
  switch (i) {
    case 0:
    case 1: {
      WorkRequest r = make_request(RequestKind::BleDiscover);
      r.active_scan = (i == 1);
      submit_request(r, i == 1 ? "BLE discovery (active)" : "BLE discovery");
      break;
    }
    case 2: nav_go(Screen::BleResults); break;
    default: break;
  }
}

void open_help() {
  if (s_cur == Screen::Help) return;
  s_help_from = s_cur;
  s_help_saved_scroll = s_scroll;
  nav_go(Screen::Help);
}

void open_module(modules::Id id) {
  const modules::Ops& o = modules::ops(id);
  const char* why = nullptr;
  if (o.available != nullptr && !o.available(&why)) {
    show_message("!Unavailable", why != nullptr ? why : "Not available right now.");
    return;
  }
  s_module = id;
  s_view_page = 0xFF;
  modules::open(id);
  nav_go(Screen::Module);
}

// The view of the open module, refreshed once per redraw. A new page resets selection and scroll and, for a
// confirmation, restarts the key guard (a key pressed before the page was readable never answers it).
void refresh_module_view(uint32_t now) {
  modules::view(s_module, s_view);
  if (s_view.page_id != s_view_page) {
    s_view_page = s_view.page_id;
    s_nav[sidx(Screen::Module)] = ListNav{};
    s_scroll = 0;
    if (s_view.kind == modules::PageKind::Confirm) s_module_guard_until = now + kConfirmGuardMs;
    if (s_view.kind == modules::PageKind::Entry) open_module_entry(s_view);
  } else if (s_view.kind == modules::PageKind::Entry && s_cur == Screen::Module) {
    // Back out of the entry: the module still shows its entry page, so leave that page for it.
    if (modules::back(s_module)) {
      modules::view(s_module, s_view);
      s_view_page = s_view.page_id;
    } else {
      nav_back();
    }
  }
}

void home_action(uint8_t idx) {
  if (idx >= kHomeCount) return;
  switch (kHome[idx].item) {
    case HomeItem::Security: nav_go(Screen::Security); break;
    case HomeItem::OysterAi: nav_go(Screen::AskOyster); break;
    case HomeItem::Settings: nav_go(Screen::Settings); break;
    case HomeItem::Help: open_help(); break;
    case HomeItem::WifiScan: open_module(modules::Id::WifiRecon); break;
    case HomeItem::ControlDeck: open_module(modules::Id::Deck); break;
    case HomeItem::SmartHome: open_module(modules::Id::SmartHome); break;
    case HomeItem::OriginalApps: open_module(modules::Id::Original); break;
  }
}

void assess_action(uint16_t i) {
  if (i >= s_acount) return;
  const AssessItem& it = s_aitems[i];
  const EnrolledTarget* t = assess_target();
  const bool needs_target = it.kind == AItem::Inspect || it.kind == AItem::Read;
  if (needs_target && t == nullptr) {
    show_message("!No enrolled target",
                 "Enrol your own test peripheral over USB (TARGET-ADD, see docs/PROVISIONING.md). "
                 "The device only connects to enrolled targets.");
    return;
  }
  switch (it.kind) {
    case AItem::Target:
      if (s_target_count > 1) {
        s_assess_target = static_cast<uint8_t>((s_assess_target + 1) % s_target_count);
        s_dirty_content = true;
      }
      break;
    case AItem::Inspect: {
      if (!target_allows(*t, Operation::BleInspectGatt)) {
        show_message("!Not allowed", "ble_inspect_gatt is not in this target's enrolled scope.");
        return;
      }
      WorkRequest r = make_request(RequestKind::InspectGatt);
      copy_str(r.target_id, sizeof(r.target_id), t->target_id);
      submit_request(r, "Inspect GATT");
      break;
    }
    case AItem::Read: {
      if (std::strcmp(t->profile_id, lab::kProfileId) != 0) {
        char msg[160];
        std::snprintf(msg, sizeof(msg), "Profile %s is not supported by this build (only %s).", t->profile_id,
                      lab::kProfileId);
        show_message("!Unsupported profile", msg);
        return;
      }
      if (!target_allows(*t, Operation::BleReadProfileValue)) {
        show_message("!Not allowed", "ble_read_profile_value is not in this target's enrolled scope.");
        return;
      }
      WorkRequest r = make_request(RequestKind::ReadValue);
      copy_str(r.target_id, sizeof(r.target_id), t->target_id);
      copy_str(r.characteristic_id, sizeof(r.characteristic_id), lab::kChars[it.idx].id);
      char label[48];
      std::snprintf(label, sizeof(label), "Read %s", lab::kChars[it.idx].id);
      submit_request(r, label);
      break;
    }
    case AItem::GattResults: nav_go(Screen::GattTree); break;
    case AItem::LastRead: show_detail(Screen::Detail, "Last read", build_read, nullptr); break;
    case AItem::NotInBuild: {
      char msg[200];
      std::snprintf(msg, sizeof(msg),
                    "%s is an M4 test. This firmware build does not run it; a backend job for it is "
                    "answered as unsupported.",
                    kM4Names[it.idx & 3]);
      show_message("Not in this build", msg);
      break;
    }
  }
}

// ---- voice question (voice_capture.h) -------------------------------------------------------------------------
// The recording page is drawn inside Screen::AskOyster while voice::state() is not Idle; the worker posts a
// BackendStatusChanged event (with the real state) every 500 ms so the page refreshes without a timer here.

bool s_voice_announced = false;  // the LED announcement for the current result was played

bool voice_page_shown() {
  voice::Status st;
  voice::status(st);
  return st.state != voice::State::Idle;
}

// The question entry, pre-filled for review (Enter sends it like a typed question).
void open_entry_with_text(const char* text) {
  open_entry(EntryPurpose::Question);
  copy_str(s_entry, sizeof(s_entry), text);
  s_entry_len = std::strlen(s_entry);
  s_entry_cursor = s_entry_len;
}

void start_voice_question() {
  if (!ai_available()) {
    show_ai_unavailable();
    return;
  }
  if (!s_session_ok) {
    show_message("!No session",
                 "No evidence session is open, so the question could not be recorded. "
                 "Check the SD card, then use Settings > New session.");
    return;
  }
  if (!voice::start()) {
    toast("Busy - wait or Fn+` to cancel");
    return;
  }
  s_voice_announced = false;
  mark_all();
}

// Keys on the recording page. Enter stops early (Recording), reviews (Ready) or leaves (Failed); Back cancels
// or discards. Other keys are ignored.
void voice_input(NavEvent e) {
  voice::Status st;
  voice::status(st);
  switch (st.state) {
    case voice::State::Starting:
    case voice::State::Recording:
      if (e == NavEvent::Select) {
        voice::request_stop();
        toast("Stopping, transcribing...");
      } else if (e == NavEvent::Back) {
        voice::request_cancel();
        toast("Recording cancelled");
      }
      break;
    case voice::State::Finishing:
      if (e == NavEvent::Back) {
        voice::request_cancel();
        toast("Cancelled; the transcript is dropped");
      }
      break;
    case voice::State::Ready:
    case voice::State::Failed: {
      if (e != NavEvent::Select && e != NavEvent::Back) return;
      char text[cfg::kMaxQuestionLen + 1];
      bool ok = false;
      if (!voice::take_result(text, sizeof(text), &ok)) break;
      if (ok && e == NavEvent::Select) {
        open_entry_with_text(text);
      } else if (ok) {
        toast("Transcript discarded");
      }
      break;
    }
    default: break;
  }
  mark_all();
}

void render_voice(const voice::Status& st) {
  char body[300];
  switch (st.state) {
    case voice::State::Starting:
      ui::draw_message("Speak a question", "Starting the microphone and opening the upload...\nDel cancels.");
      break;
    case voice::State::Recording: {
      char bar[12];
      const unsigned filled = st.level_pct / 10;
      for (unsigned i = 0; i < 10; ++i) bar[i] = i < filled ? '#' : '.';
      bar[10] = '\0';
      std::snprintf(body, sizeof(body),
                    "Recording  %lu s / %lu s\nLevel [%s] %u%%\nSent %lu KB%s\nEnter: stop and transcribe\nDel: cancel, nothing kept",
                    static_cast<unsigned long>(st.elapsed_ms / 1000), static_cast<unsigned long>(st.max_ms / 1000), bar,
                    static_cast<unsigned>(st.level_pct), static_cast<unsigned long>(st.sent_bytes / 1024),
                    st.dropped ? ", gaps!" : "");
      ui::draw_message("Speak now", body);
      break;
    }
    case voice::State::Finishing:
      std::snprintf(body, sizeof(body), "Transcribing...\nSent %lu KB (%lu.%lu s of audio)%s\nWaiting for the backend.\nDel cancels.",
                    static_cast<unsigned long>(st.sent_bytes / 1024), static_cast<unsigned long>(st.elapsed_ms / 1000),
                    static_cast<unsigned long>((st.elapsed_ms % 1000) / 100), st.dropped ? "\n!Audio gaps: upload was slow" : "");
      ui::draw_message("Speak a question", body);
      break;
    case voice::State::Ready:
    case voice::State::Failed: {
      // The result stays in voice_capture until taken by a key, so this page can be drawn again and again.
      const bool ok = st.state == voice::State::Ready;
      if (!s_voice_announced) {
        s_voice_announced = true;
        statusled::announce(ok ? statusled::Announce::Notice : statusled::Announce::Failure);
      }
      char text[cfg::kMaxQuestionLen + 1];
      voice::peek_result(text, sizeof(text));
      if (ok) {
        std::snprintf(body, sizeof(body), "%s", text);  // the first lines; the entry shows all of it
        ui::draw_message("Transcript: Enter to review", body);
      } else {
        std::snprintf(body, sizeof(body), "%s\nEnter or Del: back to the list.", text);
        ui::draw_message("!Voice question failed", body);
      }
      break;
    }
    default: break;
  }
}

void ask_action(uint16_t i) {
  switch (i) {
    case 0:
      if (ai_available()) {
        open_entry(EntryPurpose::Question);
      } else {
        show_ai_unavailable();
      }
      break;
    case 1: start_voice_question(); break;
    case 2: s_scroll = 0; nav_go(Screen::RunView); break;
    case 3: {
      if (!s_runtime || g_backend.latest_run().run_id[0] == '\0') {
        toast("No AI run to cancel");
        break;
      }
      submit_request(make_request(RequestKind::CancelRun), "Cancel AI run", true);
      break;
    }
    case 4: submit_request(make_request(RequestKind::SyncNow), "Sync now", true); break;
    default: break;
  }
}

void set_brightness_idx(int idx) {
  if (idx < 0) idx = 0;
  if (idx >= kBrightnessLevels) idx = kBrightnessLevels - 1;
  s_brightness_idx = static_cast<uint8_t>(idx);
  g_board.set_brightness(kBrightness[s_brightness_idx]);
  s_dirty_content = true;
}

void new_session() {
  if (runner_busy() || op_in_flight()) {
    toast("Busy - finish or cancel first");
    return;
  }
  g_evidence.close_session();
  if (open_session_now()) {
    s_start_fail = static_cast<uint8_t>(s_start_fail & ~kStartSession);
    s_dirty_status = s_dirty_content = true;
    toast("New session %s", strip_prefix(s_session, "sess-"));
  } else {
    show_message("!Session error", "Could not open a new evidence session. See Evidence and Settings > Storage.");
  }
}

void settings_action(uint16_t i) {
  switch (i) {
    case 0: open_module(modules::Id::WifiSetup); break;
    case 1:
      // Built once on open: the config/CA queries touch NVS and LittleFS.
      load_config();
      build_backend(s_live_body, sizeof(s_live_body));
      show_detail(Screen::Detail, "Backend", nullptr, s_live_body);
      break;
    case 2: set_brightness_idx((s_brightness_idx + 1) % kBrightnessLevels); break;
    case 3: show_detail(Screen::Detail, "Storage", build_storage, nullptr); break;
    case 4: show_detail(Screen::Detail, "Diagnostics", build_diag, nullptr); break;
    case 5: submit_request(make_request(RequestKind::SyncNow), "Sync now", true); break;
    case 6: new_session(); break;
    case 7: show_detail(Screen::Detail, "Keyboard", build_keyboard, nullptr); break;
    case 8:
      s_kt = KeyTestState{};
      nav_go(Screen::KeyTest);
      break;
    case 9: show_detail(Screen::Detail, "About", build_about, nullptr); break;
    default: break;
  }
}

// ---- key tester (P11) ----------------------------------------------------------------------------------

void keytest_record(const KeySnapshot& k, const NavOutput& nav) {
  // Modifier-only changes (Fn pressed first, keys released) keep the last result on screen.
  const bool key_edge = k.esc || k.enter || k.backspace || k.del || k.tab || k.space || k.up || k.down || k.left ||
                        k.right || k.nchars > 0;
  if (!key_edge && nav.count == 0) return;
  copy_str(s_kt.before, sizeof(s_kt.before), s_kt.action);
  Body keys = body_of(s_kt.keys, sizeof(s_kt.keys));
  const struct {
    bool on;
    const char* name;
  } flags[] = {{k.fn, "Fn"},       {k.ctrl, "Ctrl"},    {k.shift, "Shift"}, {k.opt, "Opt"},     {k.alt, "Alt"},
               {k.esc, "Esc"},     {k.enter, "Enter"},  {k.backspace, "Del"}, {k.del, "DelFwd"}, {k.tab, "Tab"},
               {k.space, "Space"}, {k.up, "Fn-Up"},     {k.down, "Fn-Down"}, {k.left, "Fn-Left"}, {k.right, "Fn-Right"}};
  for (const auto& f : flags) {
    if (f.on) keys.add("%s ", f.name);
  }
  const uint8_t nc = k.nchars < sizeof(k.chars) ? k.nchars : sizeof(k.chars);
  for (uint8_t i = 0; i < nc; ++i) {
    const char c = k.chars[i];
    if (c >= 0x21 && c <= 0x7E) {
      keys.add("'%c' ", c);
    } else {
      keys.add("0x%02x ", static_cast<unsigned>(static_cast<uint8_t>(c)));
    }
  }
  if (keys.len == 0) keys.add("(released)");
  Body act = body_of(s_kt.action, sizeof(s_kt.action));
  for (uint8_t i = 0; i < nav.count && i < 8; ++i) {
    if (nav.events[i] == NavEvent::Char) {
      act.add("Char ");
    } else {
      act.add("%s ", nav_event_name(nav.events[i]));
    }
  }
  if (act.len == 0) act.add("none");
  if (s_kt.presses < 0xFFFF) ++s_kt.presses;
  s_dirty_content = true;
}

// Every logical action is only displayed; Back or Home twice in a row leaves (Cancel stays global).
void keytest_input(NavEvent e, uint32_t now) {
  const bool leave_key = e == NavEvent::Back || e == NavEvent::Home;
  if (leave_key && s_kt.armed == e && before(now, s_kt.armed_until)) {
    s_kt.armed = NavEvent::None;
    if (e == NavEvent::Back) {
      nav_back();
    } else {
      nav_home();
    }
    return;
  }
  s_kt.armed = leave_key ? e : NavEvent::None;
  s_kt.armed_until = now + kKeyTestLeaveMs;
}

// ---- per-screen input ------------------------------------------------------------------------------------

bool progress_visible() {
  return (s_cur == Screen::WifiSurvey || s_cur == Screen::BleExplorer || s_cur == Screen::Assess) && op_in_flight();
}

bool is_message_screen(Screen s) {
  return s == Screen::Detail || s == Screen::Message || s == Screen::Evidence || s == Screen::RunView ||
         s == Screen::Help || s == Screen::Unavailable;
}

void scroll_message(NavEvent e) {
  const uint16_t max = s_msg_total > kMsgRows ? s_msg_total - kMsgRows : 0;
  uint16_t s = s_scroll;
  switch (e) {
    case NavEvent::Up: s = s > 0 ? s - 1 : 0; break;
    case NavEvent::Down: s = s < max ? s + 1 : max; break;
    case NavEvent::Left: s = s > kMsgRows ? s - kMsgRows : 0; break;
    case NavEvent::Right: s = (s + kMsgRows < max) ? s + kMsgRows : max; break;
    default: return;
  }
  if (s != s_scroll) {
    s_scroll = s;
    s_dirty_content = true;
  }
}

void entry_input(NavEvent e, char c) {
  switch (e) {
    case NavEvent::Char:
      if (s_entry_len < s_entry_max && s_entry_len < sizeof(s_entry) - 1 && entry_accepts(c)) {
        std::memmove(s_entry + s_entry_cursor + 1, s_entry + s_entry_cursor, s_entry_len - s_entry_cursor + 1);
        s_entry[s_entry_cursor++] = c;
        ++s_entry_len;
        s_dirty_content = true;
      }
      break;
    case NavEvent::Erase:
      if (s_entry_len == 0) {
        nav_back();  // Del on an empty buffer leaves the entry (no bare Esc key on this keyboard)
      } else if (s_entry_cursor > 0) {
        std::memmove(s_entry + s_entry_cursor - 1, s_entry + s_entry_cursor, s_entry_len - s_entry_cursor + 1);
        --s_entry_cursor;
        --s_entry_len;
        s_dirty_content = true;
      }
      break;
    case NavEvent::Left:
      if (s_entry_cursor > 0) --s_entry_cursor;
      s_dirty_content = true;
      break;
    case NavEvent::Right:
      if (s_entry_cursor < s_entry_len) ++s_entry_cursor;
      s_dirty_content = true;
      break;
    case NavEvent::Up: {
      constexpr size_t kLine = ui::kTextCols;  // one line of draw_text_entry
      s_entry_cursor = s_entry_cursor >= kLine ? s_entry_cursor - kLine : 0;
      s_dirty_content = true;
      break;
    }
    case NavEvent::Down: {
      constexpr size_t kLine = ui::kTextCols;
      s_entry_cursor = s_entry_cursor + kLine <= s_entry_len ? s_entry_cursor + kLine : s_entry_len;
      s_dirty_content = true;
      break;
    }
    case NavEvent::Select: submit_entry(); break;
    case NavEvent::Back: nav_back(); break;
    default: break;
  }
}

void dispatch(NavEvent e, char c) {
  const uint32_t now = millis();
  ListNav& nv = s_nav[sidx(s_cur)];
  switch (s_cur) {
    case Screen::Boot:
    case Screen::BoardError:
    case Screen::Provisioning:
      return;

    case Screen::MainMenu:
      if (e == NavEvent::Select) {
        home_action(static_cast<uint8_t>(nv.sel));
      } else if (e == NavEvent::Char && c >= '1' && c <= '0' + kHomeCount) {
        for (uint8_t i = 0; i < kHomeCount; ++i) {
          if (kHome[i].number != static_cast<uint8_t>(c - '0')) continue;
          nv.sel = i;
          tile_scroll(nv, kHomeCols);
          home_action(i);
          break;
        }
      } else {
        tile_move(nv, kHomeCount, kHomeCols, e);
      }
      return;

    case Screen::Security:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select) {
        if (nv.sel < kSecCount) nav_go(kSecScreens[nv.sel]);
      } else {
        tile_move(nv, kSecCount, 1, e);
      }
      return;

    case Screen::WifiSurvey:
    case Screen::BleExplorer:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (!progress_visible()) {
        const bool wifi = s_cur == Screen::WifiSurvey;
        if (e == NavEvent::Select) {
          if (wifi) {
            wifi_menu_action(nv.sel);
          } else {
            ble_menu_action(nv.sel);
          }
        } else {
          list_move(nv, wifi ? kWifiMenuCount : kBleMenuCount, e);
        }
      }
      return;

    case Screen::Targets:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select && s_target_count > 0) {
        show_detail(Screen::Detail, "Enrolled target", build_target_detail, nullptr);
      } else if (e == NavEvent::Select) {
        show_message("No enrolled targets",
                     "Enrol your own test peripheral over USB (TARGET-ADD, docs/PROVISIONING.md). "
                     "The device never connects to targets that are not enrolled locally.");
      } else if (e == NavEvent::Char && (c == 'a' || c == 'A') && s_target_count > 0) {
        s_assess_target = static_cast<uint8_t>(nv.sel);
        nav_go(Screen::Assess);
      } else {
        list_move(nv, s_target_count, e);
      }
      return;

    case Screen::Assess:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (!progress_visible()) {
        const bool on_target = nv.sel < s_acount && s_aitems[nv.sel].kind == AItem::Target;
        if (on_target && (e == NavEvent::Left || e == NavEvent::Right) && s_target_count > 1) {
          const int d = e == NavEvent::Right ? 1 : s_target_count - 1;
          s_assess_target = static_cast<uint8_t>((s_assess_target + d) % s_target_count);
          s_dirty_content = true;
        } else if (e == NavEvent::Select) {
          assess_action(nv.sel);
        } else {
          list_move(nv, s_acount, e);
        }
      }
      return;

    case Screen::AskOyster:
      if (voice_page_shown()) {
        voice_input(e);
      } else if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select) {
        ask_action(nv.sel);
      } else if (e == NavEvent::Char && (c == 'v' || c == 'V')) {
        start_voice_question();
      } else {
        list_move(nv, kAskCount, e);
      }
      return;

    case Screen::Settings:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (nv.sel == 2 && (e == NavEvent::Left || e == NavEvent::Right)) {
        set_brightness_idx(static_cast<int>(s_brightness_idx) + (e == NavEvent::Right ? 1 : -1));
      } else if (e == NavEvent::Select) {
        settings_action(nv.sel);
      } else {
        list_move(nv, kSettingsCount, e);
      }
      return;

    case Screen::WifiResults:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select && s_wifi_sum.count > 0) {
        open_ap_detail(nv.sel);
      } else if (e == NavEvent::Char && (c == 's' || c == 'S')) {
        s_wifi_sort = s_wifi_sort == WifiSort::Rssi ? WifiSort::Channel : WifiSort::Rssi;
        g_results.set_wifi_sort(s_wifi_sort);
        nv.sel = nv.top = 0;
        s_dirty_content = true;
      } else {
        list_move(nv, s_wifi_sum.count, e);
      }
      return;

    case Screen::BleResults:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select && s_ble_sum.count > 0) {
        open_ble_detail(nv.sel);
      } else {
        list_move(nv, s_ble_sum.count, e);
      }
      return;

    case Screen::GattTree:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select && s_gatt_sum.count > 0) {
        open_gatt_detail(nv.sel);
      } else {
        list_move(nv, s_gatt_sum.count, e);
      }
      return;

    case Screen::Help:
      if (e == NavEvent::Back) {
        nav_back();
        s_scroll = s_help_saved_scroll;  // the screen below keeps its scroll position
      } else if (e == NavEvent::Select) {
        nav_go(Screen::LedLegend);
      } else {
        scroll_message(e);
      }
      return;

    case Screen::LedLegend:
      if (e == NavEvent::Back) {
        nav_back();
      } else {
        list_move(nv, kLegendCount, e);
      }
      return;

    case Screen::KeyTest:  // handled by keytest_input() in handle_input()
      return;

    case Screen::Module: {
      const modules::PageView& v = s_view;
      s_dirty_content = s_dirty_footer = true;
      if (v.kind == modules::PageKind::Confirm) {
        if (before(now, s_module_guard_until)) return;  // keys typed before the page was readable
        if (e == NavEvent::Select) {
          modules::select(s_module, 0);
        } else if (e == NavEvent::Back && !modules::back(s_module)) {
          nav_back();
        }
        return;
      }
      if (e == NavEvent::Back) {
        if (!modules::back(s_module)) nav_back();
      } else if (modules::key(s_module, e, c)) {
        // handled by the module (refresh, number keys, ...)
      } else if (v.kind == modules::PageKind::List) {
        if (e == NavEvent::Select && v.count > 0) {
          modules::select(s_module, nv.sel);
        } else {
          list_move(nv, v.count, e);
        }
      } else if (e == NavEvent::Select) {
        modules::select(s_module, 0);
      } else {
        scroll_message(e);
      }
      return;
    }

    case Screen::Detail:
    case Screen::Message:
    case Screen::Evidence:
    case Screen::RunView:
    case Screen::Unavailable:
      if (e == NavEvent::Back) {
        nav_back();
      } else if (e == NavEvent::Select && s_cur == Screen::Detail && s_detail_builder == build_target_detail) {
        s_assess_target = static_cast<uint8_t>(s_nav[sidx(Screen::Targets)].sel);
        nav_go(Screen::Assess);
      } else {
        scroll_message(e);
      }
      return;

    case Screen::ConfirmDialog:
      if (before(now, s_pc_guard_until)) return;  // ignore keys typed before the dialog was readable
      if (e == NavEvent::Select) {
        resolve_pending(true);
      } else if (e == NavEvent::Back) {
        resolve_pending(false);
      }
      return;

    case Screen::TextEntry:
      entry_input(e, c);
      return;
  }
}

// ---- events ----------------------------------------------------------------------------------------------

void handle_event(const AppEvent& ev) {
  switch (ev.type) {
    case AppEventType::OperationStarted:
      s_op.active = true;
      s_op.requested = false;
      s_op.op = ev.a <= static_cast<uint32_t>(Operation::Unknown) ? static_cast<Operation>(ev.a) : Operation::Unknown;
      s_op.items = 0;
      s_op.started_ms = millis();
      if (ev.text[0]) {
        copy_str(s_op.label, sizeof(s_op.label), ev.text);
      } else {
        copy_str(s_op.label, sizeof(s_op.label), op_name(s_op.op));
      }
      mark_all();
      break;
    case AppEventType::OperationProgress:
      s_op.items = ev.b;
      if (progress_visible()) s_dirty_content = true;
      break;
    case AppEventType::OperationFinished: {
      const Operation op = ev.a <= static_cast<uint32_t>(Operation::Unknown) ? static_cast<Operation>(ev.a) : Operation::Unknown;
      s_op.active = false;
      s_op.requested = false;
      std::snprintf(s_last_result, sizeof(s_last_result), "%s: %s%s%.30s", op_name(op), status_name(ev.b),
                    ev.text[0] ? " - " : "", ev.text);
      toast("%s", s_last_result);
      const bool ok = ev.b == static_cast<uint32_t>(EventStatus::Success) || ev.b == static_cast<uint32_t>(EventStatus::Partial);
      statusled::announce(ok ? statusled::Announce::Success : statusled::Announce::Failure);
      refresh_counts();
      // Show the results of an operation the user is watching.
      if (ok && (s_cur == Screen::WifiSurvey || s_cur == Screen::BleExplorer || s_cur == Screen::Assess)) {
        if (op == Operation::WifiSurvey) {
          nav_go(Screen::WifiResults);
        } else if (op == Operation::BleDiscover) {
          nav_go(Screen::BleResults);
        } else if (op == Operation::BleInspectGatt) {
          nav_go(Screen::GattTree);
        } else if (op == Operation::BleReadProfileValue) {
          show_detail(Screen::Detail, "Last read", build_read, nullptr);
        }
      }
      mark_all();
      break;
    }
    case AppEventType::ResultsUpdated:
      refresh_counts();
      if (ev.a == static_cast<uint32_t>(ResultKind::Ble)) s_seen_version = 0xFFFFFFFFu;
      s_dirty_content = true;
      break;
    case AppEventType::BackendStatusChanged:
      s_dirty_status = true;
      if (s_cur == Screen::AskOyster || s_cur == Screen::RunView || s_cur == Screen::Settings ||
          s_cur == Screen::MainMenu) {
        s_dirty_content = true;  // the Oyster AI tile shows the backend state
      }
      break;
    case AppEventType::ConfirmationRequested:
      s_last_poll_ms = millis() - kPollMs;  // poll the runner on this tick
      statusled::announce(statusled::Announce::Notice);
      break;
    case AppEventType::ConfirmationClosed:
      if (s_pc_shown) {
        s_pc_shown = false;
        if (s_cur == Screen::ConfirmDialog) nav_back();
        toast("Confirmation %s", ev.a == 1 ? "approved" : (ev.a == 2 ? "expired" : "rejected"));
      }
      break;
    case AppEventType::RunUpdated: {
      const RunSummary& run = g_backend.latest_run();
      const bool ended = std::strcmp(run.status, "succeeded") == 0 || std::strcmp(run.status, "failed") == 0;
      if (ended && std::strcmp(run.status, s_run_seen_status) != 0) {
        copy_str(s_run_seen_status, sizeof(s_run_seen_status), run.status);
        if (s_cur != Screen::RunView) {
          s_answer_unseen = true;  // blue until the answer is opened
          statusled::announce(statusled::Announce::Notice);
        }
      } else if (!ended) {
        s_run_seen_status[0] = 0;
      }
      if (s_op.requested) s_op.requested = false;
      if (s_cur == Screen::RunView) s_dirty_content = true;
      s_dirty_footer = s_dirty_status = true;  // the banner follows the latest run status
      break;
    }
    case AppEventType::StorageChanged:
      if (ev.a <= static_cast<uint32_t>(SdState::Full)) toast("SD: %s", to_string(static_cast<SdState>(ev.a)));
      s_dirty_status = true;
      if (s_cur == Screen::Evidence || s_cur == Screen::Settings) s_dirty_content = true;
      break;
    case AppEventType::ModuleChanged:
      if (s_cur == Screen::Module || s_cur == Screen::MainMenu) s_dirty_content = s_dirty_footer = true;
      break;
    case AppEventType::Toast:
      toast("%.60s", ev.text);
      // The runner reports a refused request (no session, not enrolled, radio busy) with a toast only.
      if (s_op.requested && !s_op.active && !runner_busy()) {
        s_op.requested = false;
        mark_all();
      }
      break;
  }
}

// ---- periodic state polling ------------------------------------------------------------------------------

void poll_state(uint32_t now) {
  if (s_cur != Screen::BoardError) {
    const bool prov = g_provisioning.active();
    if (prov && s_cur != Screen::Provisioning) {
      nav_go(Screen::Provisioning);
    } else if (!prov && s_cur == Screen::Provisioning) {
      nav_back();
      reload_config_state();
      toast("USB setup ended");
    }
    // FACTORY-RESET / DONE changed the stored setup while the session may still be open.
    if (g_provisioning.config_generation() != s_cfg_gen) {
      reload_config_state();
      toast(s_cfg.provisioned ? "Setup changed over USB" : "Device NOT provisioned");
    }
  }
  poll_confirmation(now);

  if (s_op.requested && !runner_busy() && now - s_op.requested_ms > kRequestStartTimeoutMs) {
    s_op.requested = false;  // finished between polls or never started; the outcome toast covers it
    mark_all();
  }
  // A dropped OperationFinished event must not leave the UI blocked: clear after the runner is idle.
  if (s_op.active && !runner_busy()) {
    if (s_op_idle_since == 0) {
      s_op_idle_since = now | 1u;
    } else if (now - s_op_idle_since > kStaleOpMs) {
      s_op.active = false;
      s_op_idle_since = 0;
      mark_all();
    }
  } else {
    s_op_idle_since = 0;
  }

  if (s_runtime) {
    bool changed = false;
    for (uint8_t k = 0; k < 4; ++k) {
      const uint32_t v = g_results.version(static_cast<ResultKind>(k));
      if (v != s_ver[k]) {
        s_ver[k] = v;
        changed = true;
      }
    }
    if (changed) {
      refresh_counts();
      s_dirty_content = true;
    }
  }

  const ModeBanner mode = current_mode();
  const BackendState be = s_runtime ? g_backend.state() : BackendState::Offline;
  if (mode != s_prev_mode || be != s_prev_backend) {
    s_prev_mode = mode;
    s_prev_backend = be;
    s_dirty_status = true;
    if (s_cur == Screen::MainMenu) s_dirty_content = true;  // tile status lines follow radio and backend
  }
  const bool busy = runner_busy();
  if (busy != s_prev_busy) {
    s_prev_busy = busy;
    s_dirty_footer = s_dirty_status = true;
    if (progress_visible()) s_dirty_content = true;
  }
}

// ---- drawing ------------------------------------------------------------------------------------------------

void render_message_screen() {
  const char* title = s_detail_title;
  char* body = s_detail_body;
  if (s_cur == Screen::Evidence) {
    build_evidence(s_live_body, sizeof(s_live_body));
    title = "Saved Findings";
    body = s_live_body;
  } else if (s_cur == Screen::RunView) {
    build_run(s_live_body, sizeof(s_live_body));
    title = ai_available() ? "Ask Oyster run" : "!Ask Oyster: AI unavailable";
    body = s_live_body;
  } else if (s_cur == Screen::Help) {
    build_help(s_live_body, sizeof(s_live_body));
    title = "Help";
    body = s_live_body;
  } else if (s_cur == Screen::Module) {
    copy_str(s_live_body, sizeof(s_live_body), s_view.body);
    title = s_view.title;
    body = s_live_body;
  } else if (s_cur == Screen::Unavailable) {
    const UnavailableModule& m = kUnavailable[s_unavailable < kUnavailableCount ? s_unavailable : 0];
    copy_str(s_live_body, sizeof(s_live_body), m.body);
    title = m.title;
    body = s_live_body;
  } else if (s_detail_builder != nullptr) {
    s_detail_builder(s_detail_body, sizeof(s_detail_body));
  }
  s_msg_total = ui::wrapped_line_count(body);
  const uint16_t max = s_msg_total > kMsgRows ? s_msg_total - kMsgRows : 0;
  if (s_scroll > max) s_scroll = max;
  ui::draw_message(title, ui::skip_wrapped_lines(body, s_scroll), s_scroll, s_msg_total);
}

// Home grid (H01) with live status lines: connectivity derives from the real backend state.
void render_home(const ListNav& nv) {
  char details[kHomeCount][16];
  ui::Tile tiles[kHomeCount];
  for (uint8_t i = 0; i < kHomeCount; ++i) {
    const HomeTile& h = kHome[i];
    bool enabled = true;
    const char* what = "";
    modules::Id mod = modules::Id::Hosts;
    bool has_mod = false;
    char mod_text[16] = {};
    switch (h.item) {
      case HomeItem::Security:
        what = (current_mode() == ModeBanner::LiveRadio) ? "scanning" : "Wi-Fi, BLE";
        break;
      case HomeItem::OysterAi: what = backend_short(); break;
      case HomeItem::Settings: what = "device"; break;
      case HomeItem::Help: what = "keys"; break;
      case HomeItem::WifiScan: mod = modules::Id::WifiRecon; has_mod = true; break;
      case HomeItem::ControlDeck: mod = modules::Id::Deck; has_mod = true; break;
      case HomeItem::SmartHome: mod = modules::Id::SmartHome; has_mod = true; break;
      case HomeItem::OriginalApps: mod = modules::Id::Original; has_mod = true; break;
    }
    if (has_mod) {
      modules::tile_status(mod, mod_text, sizeof(mod_text));
      what = mod_text;
      const modules::Ops& o = modules::ops(mod);
      const char* why = nullptr;
      enabled = o.available == nullptr || o.available(&why);
    }
    std::snprintf(details[i], sizeof(details[i]), "%u %.11s", static_cast<unsigned>(h.number), what);
    tiles[i] = ui::Tile{h.icon, h.label, details[i], enabled};
  }
  char title[80];
  if (s_start_fail != 0) {
    char names[64];
    startup_names(names, sizeof(names));
    std::snprintf(title, sizeof(title), "!FAILED: %s", names);
  } else {
    std::snprintf(title, sizeof(title), "%s", (s_cfg_ok && s_cfg.provisioned) ? "OYSTER OS" : "OYSTER OS (setup!)");
  }
  const uint8_t sel = static_cast<uint8_t>(nv.sel < kHomeCount ? nv.sel : 0);
  char right[8];
  std::snprintf(right, sizeof(right), "%u/%u", static_cast<unsigned>(kHome[sel].number), static_cast<unsigned>(kHomeCount));
  ui::draw_tiles(ui::TileModel{title, right, tiles, kHomeCount, kHomeCols, sel, static_cast<uint8_t>(nv.top)});
}

// Security (S10): the implemented tools with counts from the result cache.
void render_security(const ListNav& nv) {
  char details[kSecCount][40];
  ui::Tile tiles[kSecCount];
  for (uint8_t i = 0; i < kSecCount; ++i) {
    bool enabled = true;
    switch (kSecScreens[i]) {
      case Screen::WifiSurvey:
        if (s_ver[static_cast<uint8_t>(ResultKind::Wifi)] == 0) {
          std::snprintf(details[i], sizeof(details[i]), "passive 2.4 GHz survey");
        } else {
          std::snprintf(details[i], sizeof(details[i]), "%u networks in the last survey", static_cast<unsigned>(s_count_wifi));
        }
        break;
      case Screen::BleExplorer:
        if (s_start_fail & kStartBle) {
          std::snprintf(details[i], sizeof(details[i]), "BLE did not start");
          enabled = false;
        } else if (s_ver[static_cast<uint8_t>(ResultKind::Ble)] == 0) {
          std::snprintf(details[i], sizeof(details[i]), "advertisement discovery");
        } else {
          std::snprintf(details[i], sizeof(details[i]), "%u advertisers in the last scan", static_cast<unsigned>(s_count_ble));
        }
        break;
      case Screen::Evidence: std::snprintf(details[i], sizeof(details[i]), "evidence log and upload state"); break;
      case Screen::Targets:
        std::snprintf(details[i], sizeof(details[i]), "%u enrolled (your own devices)", static_cast<unsigned>(s_target_count));
        break;
      case Screen::Assess: std::snprintf(details[i], sizeof(details[i]), "authorized GATT checks"); break;
      default: details[i][0] = '\0'; break;
    }
    tiles[i] = ui::Tile{kSecIcons[i], kSecLabels[i], details[i], enabled};
  }
  const uint8_t sel = static_cast<uint8_t>(nv.sel < kSecCount ? nv.sel : 0);
  char right[8];
  std::snprintf(right, sizeof(right), "%u/%u", static_cast<unsigned>(sel) + 1, static_cast<unsigned>(kSecCount));
  ui::draw_tiles(ui::TileModel{"Security", right, tiles, kSecCount, 1, sel, static_cast<uint8_t>(nv.top)});
}

// Help > LED colours: the legend, drawn with the LED's own colours (statusled::swatch) so it matches.
struct LegendEntry {
  bool announce;
  uint8_t which;  // statusled::Pattern or statusled::Announce
  bool blink;
  const char* label;
  const char* comment;
};
const LegendEntry kLegend[] = {
    {false, static_cast<uint8_t>(statusled::Pattern::Online), false, "Online, breathing", "backend reachable"},
    {false, static_cast<uint8_t>(statusled::Pattern::Attention), false, "Attention, breathing", "confirm or new answer waits"},
    {false, static_cast<uint8_t>(statusled::Pattern::Offline), true, "Offline, blink every 2 s", "not set up or no backend"},
    {false, static_cast<uint8_t>(statusled::Pattern::Connecting), false, "Connecting, breathing", "joining Wi-Fi, reaching backend"},
    {false, static_cast<uint8_t>(statusled::Pattern::Error), true, "Error, blink every 1 s", "certificate, token or start-up"},
    {false, static_cast<uint8_t>(statusled::Pattern::LiveRadio), false, "Scanning, breathing", "Wi-Fi survey or Bluetooth runs"},
    {false, static_cast<uint8_t>(statusled::Pattern::Boot), false, "Boot, breathing", "start screen, press any key"},
    {true, static_cast<uint8_t>(statusled::Announce::Success), true, "Finished, 2 pulses", "an operation completed"},
    {true, static_cast<uint8_t>(statusled::Announce::Failure), true, "Failed, 2 long blinks", "an operation failed"},
    {true, static_cast<uint8_t>(statusled::Announce::Notice), true, "Notice, 3 quick blinks", "a request or answer arrived"},
};
static_assert(sizeof(kLegend) / sizeof(kLegend[0]) == kLegendCount, "legend row count");

void render_led_legend(const ListNav& nv) {
  ui::ColourRow rows[kLegendCount];
  for (uint8_t i = 0; i < kLegendCount; ++i) {
    uint8_t r, g, b;
    if (kLegend[i].announce) {
      statusled::swatch(static_cast<statusled::Announce>(kLegend[i].which), r, g, b);
    } else {
      statusled::swatch(static_cast<statusled::Pattern>(kLegend[i].which), r, g, b);
    }
    const uint32_t rgb = (static_cast<uint32_t>(r) << 16) | (static_cast<uint32_t>(g) << 8) | b;
    rows[i] = ui::ColourRow{theme::rgb565(rgb), kLegend[i].blink, kLegend[i].label, kLegend[i].comment};
  }
  ui::draw_colour_table("LED colours", rows, kLegendCount, static_cast<uint8_t>(nv.sel), static_cast<uint8_t>(nv.top));
}

void render_keytest() {
  build_keytest(s_live_body, sizeof(s_live_body));
  ui::draw_message("Key tester [P11]", s_live_body);
}

void render_progress(uint32_t now) {
  const char* label = s_op.label[0] ? s_op.label : "working";
  const uint32_t elapsed_ms = s_op.active ? now - s_op.started_ms : now - s_op.requested_ms;
  char line[64];
  std::snprintf(line, sizeof(line), "%s%.40s", s_op.requested && !s_op.active ? "Queued: " : "", label);
  ui::draw_progress(line, elapsed_ms / 1000, s_op.items, true);
}

void render_content(uint32_t now) {
  ListNav& nv = s_nav[sidx(s_cur)];
  switch (s_cur) {
    case Screen::Boot:
      ui::draw_message("OYSTER OS", "Starting...");
      break;
    case Screen::BoardError: {
      char text[400];
      build_board_error(text, sizeof(text));
      ui::draw_message("!BOARD MISMATCH", text);
      break;
    }
    case Screen::Provisioning: {
      char text[200];
      std::snprintf(text, sizeof(text),
                    "Provisioning session active over USB serial.\nValues are not shown on screen.\n"
                    "Stored setup: %s\nThe device reboots when setup is done.",
                    (s_cfg_ok && s_cfg.provisioned) ? "complete" : "NOT provisioned");
      ui::draw_message("USB setup", text);
      break;
    }
    case Screen::MainMenu:
      render_home(nv);
      break;
    case Screen::Security:
      render_security(nv);
      break;
    case Screen::WifiSurvey:
      if (progress_visible()) {
        render_progress(now);
      } else {
        ui::draw_list(ui::ListModel{"Wi-Fi Survey", kWifiMenuCount, nv.sel, nv.top, wifi_menu_row});
      }
      break;
    case Screen::BleExplorer:
      if (progress_visible()) {
        render_progress(now);
      } else {
        ui::draw_list(ui::ListModel{(s_start_fail & kStartBle) ? "!Bluetooth: BLE off" : "Bluetooth Explorer",
                                    kBleMenuCount, nv.sel, nv.top, ble_menu_row, ble_menu_disabled});
      }
      break;
    case Screen::Targets:
      compute_sightings(false);
      clamp_nav(nv, s_target_count);
      ui::draw_list(ui::ListModel{"Targets (hint)", s_target_count, nv.sel, nv.top, target_row, nullptr,
                                  "No enrolled targets"});
      break;
    case Screen::Assess:
      if (progress_visible()) {
        render_progress(now);
      } else {
        clamp_nav(nv, s_acount);
        ui::draw_list(ui::ListModel{"Assess", s_acount, nv.sel, nv.top, assess_row, assess_disabled});
      }
      break;
    case Screen::AskOyster: {
      voice::Status vs;
      voice::status(vs);
      if (vs.state != voice::State::Idle) {
        render_voice(vs);
        break;
      }
      ui::draw_list(ui::ListModel{ai_available() ? "Oyster AI" : "!Oyster AI: unavailable", kAskCount, nv.sel, nv.top,
                                  ask_row});
      break;
    }
    case Screen::Settings:
      ui::draw_list(ui::ListModel{"Settings", kSettingsCount, nv.sel, nv.top, settings_row});
      break;
    case Screen::WifiResults: {
      load_wifi_page();
      char title[32];
      const bool partial = !s_wifi_sum.complete || s_wifi_sum.cancelled || s_wifi_sum.overflow > 0;
      if (s_ver[static_cast<uint8_t>(ResultKind::Wifi)] == 0) {
        std::snprintf(title, sizeof(title), "Wi-Fi: no survey yet");
      } else {
        std::snprintf(title, sizeof(title), "Wi-Fi %s%s", s_wifi_sort == WifiSort::Rssi ? "by RSSI" : "by ch",
                      partial ? " PARTIAL" : "");
      }
      const bool none_yet = s_ver[static_cast<uint8_t>(ResultKind::Wifi)] == 0;
      ui::draw_list(ui::ListModel{title, s_wifi_sum.count, nv.sel, nv.top, wifi_row, nullptr,
                                  none_yet ? "Start a survey first" : "No networks seen (2.4 GHz)"});
      break;
    }
    case Screen::BleResults: {
      load_ble_page();
      char title[32];
      const bool partial = !s_ble_sum.complete || s_ble_sum.cancelled || s_ble_sum.overflow > 0;
      if (s_ver[static_cast<uint8_t>(ResultKind::Ble)] == 0) {
        std::snprintf(title, sizeof(title), "BLE: no discovery yet");
      } else {
        std::snprintf(title, sizeof(title), "BLE %s%s", s_ble_sum.active_scan ? "active" : "passive",
                      partial ? " PARTIAL" : "");
      }
      const bool none_yet = s_ver[static_cast<uint8_t>(ResultKind::Ble)] == 0;
      ui::draw_list(ui::ListModel{title, s_ble_sum.count, nv.sel, nv.top, ble_row, nullptr,
                                  none_yet ? "Start a discovery first" : "No advertisers seen"});
      break;
    }
    case Screen::GattTree: {
      load_gatt_page();
      char title[40];
      s_gatt_sum.target_id[sizeof(s_gatt_sum.target_id) - 1] = '\0';
      if (s_gatt_sum.count == 0 && s_gatt_sum.target_id[0] == '\0') {
        std::snprintf(title, sizeof(title), "GATT: not inspected");
      } else {
        std::snprintf(title, sizeof(title), "GATT%s %.14s", s_gatt_sum.complete ? "" : " PART",
                      strip_prefix(s_gatt_sum.target_id, "target-"));
      }
      ui::draw_list(ui::ListModel{title, s_gatt_sum.count, nv.sel, nv.top, gatt_row});
      break;
    }
    case Screen::Detail:
    case Screen::Message:
    case Screen::Evidence:
    case Screen::RunView:
    case Screen::Help:
    case Screen::Unavailable:
      render_message_screen();
      break;
    case Screen::Module:
      switch (s_view.kind) {
        case modules::PageKind::List:
          clamp_nav(nv, s_view.count);
          ui::draw_list(ui::ListModel{s_view.title, s_view.count, nv.sel, nv.top, s_view.row, s_view.row_disabled,
                                      s_view.empty});
          break;
        case modules::PageKind::Text:
          render_message_screen();
          break;
        case modules::PageKind::Confirm:
          ui::draw_confirm(s_view.confirm_operation, s_view.confirm_target, s_view.confirm_effect);
          break;
      }
      break;
    case Screen::LedLegend:
      clamp_nav(nv, kLegendCount);
      render_led_legend(nv);
      break;
    case Screen::KeyTest:
      render_keytest();
      break;
    case Screen::ConfirmDialog:
      s_pc.operation[sizeof(s_pc.operation) - 1] = '\0';
      s_pc.target_id[sizeof(s_pc.target_id) - 1] = '\0';
      s_pc.effect[sizeof(s_pc.effect) - 1] = '\0';
      ui::draw_confirm(s_pc.operation, s_pc.target_id, s_pc.effect);
      break;
    case Screen::TextEntry:
      if (s_entry_purpose == EntryPurpose::Module) {
        char shown[sizeof(s_entry)];
        if (s_entry_secret) {
          std::memset(shown, '*', s_entry_len);
          shown[s_entry_len] = 0;
        } else {
          copy_str(shown, sizeof(shown), s_entry);
        }
        ui::draw_text_entry(s_entry_prompt, shown, s_entry_cursor, "Del on empty text: Back");
      } else {
        ui::draw_text_entry(s_entry_purpose == EntryPurpose::RoomLabel ? "Room label" : "Ask Oyster", s_entry,
                            s_entry_cursor, "Del on empty text: Back");
      }
      break;
  }
}

void default_footer(char* left, size_t ln, char* right, size_t rn) {
  left[0] = right[0] = '\0';
  switch (s_cur) {
    case Screen::Boot: std::snprintf(left, ln, "Booting"); break;
    case Screen::BoardError: std::snprintf(left, ln, "!Wrong board - input disabled"); break;
    case Screen::Provisioning: std::snprintf(left, ln, "USB provisioning active"); break;
    // Key hints "Key:Action" (ui::draw_footer colours the key). Back is always named the same way.
    case Screen::MainMenu:
      std::snprintf(left, ln, "?:Help 1-8:Open");
      std::snprintf(right, rn, "Ent:Open");
      break;
    case Screen::WifiResults:
      std::snprintf(left, ln, "Del:Back s:Sort");
      std::snprintf(right, rn, "Ent:Info");
      break;
    case Screen::Targets:
      std::snprintf(left, ln, "Del:Back a:Assess");
      std::snprintf(right, rn, "Ent:Info");
      break;
    case Screen::Assess:
      std::snprintf(left, ln, s_target_count > 1 ? "Del:Back ,/:Target" : "Del:Back ?:Help");
      std::snprintf(right, rn, "Ent:Run");
      break;
    case Screen::Settings:
      std::snprintf(left, ln, "Del:Back ,/:Adjust");
      std::snprintf(right, rn, "Ent:Open");
      break;
    case Screen::Detail:
    case Screen::Message:
    case Screen::Evidence:
    case Screen::RunView:
    case Screen::Unavailable:
      std::snprintf(left, ln, "Del:Back ;.:Scroll");
      std::snprintf(right, rn, "%u/%u", static_cast<unsigned>(s_scroll) + 1,
                    static_cast<unsigned>(s_msg_total > 0 ? s_msg_total : 1));
      break;
    case Screen::Help:
      std::snprintf(left, ln, "Del:Back Ent:LED colours");
      std::snprintf(right, rn, "%u/%u", static_cast<unsigned>(s_scroll) + 1,
                    static_cast<unsigned>(s_msg_total > 0 ? s_msg_total : 1));
      break;
    case Screen::LedLegend:
      std::snprintf(left, ln, "Del:Back ;.:Move");
      break;
    case Screen::Module:
      std::snprintf(left, ln, "%s", s_view.left);
      std::snprintf(right, rn, "%s", s_view.right);
      if (s_view.kind == modules::PageKind::Text) {
        std::snprintf(right, rn, "%u/%u", static_cast<unsigned>(s_scroll) + 1,
                      static_cast<unsigned>(s_msg_total > 0 ? s_msg_total : 1));
      }
      break;
    case Screen::KeyTest:
      std::snprintf(left, ln, "Del Del:Leave");
      break;
    case Screen::TextEntry:
      std::snprintf(left, ln, "Ent:OK Del:Erase");
      std::snprintf(right, rn, "%u/%u", static_cast<unsigned>(s_entry_len), static_cast<unsigned>(s_entry_max));
      break;
    case Screen::AskOyster: {
      voice::Status vs;
      voice::status(vs);
      switch (vs.state) {
        case voice::State::Starting:
        case voice::State::Recording: std::snprintf(left, ln, "Ent:Stop Del:Cancel"); break;
        case voice::State::Finishing:
          std::snprintf(left, ln, "Del:Cancel");
          std::snprintf(right, rn, "transcribing");
          break;
        case voice::State::Ready: std::snprintf(left, ln, "Ent:Review Del:Discard"); break;
        case voice::State::Failed: std::snprintf(left, ln, "Ent:Back Del:Back"); break;
        default:
          std::snprintf(left, ln, "Del:Back ?:Help");
          std::snprintf(right, rn, "Ent:Open");
          break;
      }
      break;
    }
    case Screen::Security:
    case Screen::WifiSurvey:
    case Screen::BleExplorer:
      std::snprintf(left, ln, "Del:Back ?:Help");
      std::snprintf(right, rn, "Ent:Open");
      break;
    case Screen::ConfirmDialog:
    case Screen::BleResults:
    case Screen::GattTree:
      std::snprintf(left, ln, "Del:Back ?:Help");
      std::snprintf(right, rn, "Ent:Info");
      break;
  }
}

void render_footer(uint32_t now) {
  char left[48];
  char right[32];
  default_footer(left, sizeof(left), right, sizeof(right));
  const bool busy = runner_busy();
  s_footer_timer_active = false;
  if (before(now, s_cancel_ack_until)) {
    std::snprintf(left, sizeof(left), "!CANCEL REQUESTED");
    std::snprintf(right, sizeof(right), busy ? "stopping..." : "nothing running");
    s_footer_timer_active = true;
  } else if (s_cur == Screen::ConfirmDialog) {
    const uint32_t since = now - s_pc_fetched_ms;
    const uint32_t rem = s_pc.remaining_ms > since ? s_pc.remaining_ms - since : 0;
    std::snprintf(left, sizeof(left), "Ent:Approve Del:Reject");
    std::snprintf(right, sizeof(right), "%lus", static_cast<unsigned long>(rem / 1000));
    s_footer_timer_active = true;
  } else if (before(now, s_toast_until)) {
    ui::draw_notice(s_toast);
    s_footer_timer_active = true;
    return;
  } else if (op_in_flight() || busy) {
    uint32_t ms;
    const char* label;
    if (op_in_flight()) {
      ms = s_op.active ? now - s_op.started_ms : now - s_op.requested_ms;
      label = s_op.label[0] ? s_op.label : "working";
    } else {
      ms = g_runner.busy_elapsed_ms();
      label = op_name(g_runner.current_op());
    }
    std::snprintf(left, sizeof(left), "%.24s %lus", label, static_cast<unsigned long>(ms / 1000));
    std::snprintf(right, sizeof(right), "Fn+`:Cancel");
    s_footer_timer_active = true;
  }
  ui::draw_footer(left, right);
}

void draw(uint32_t now) {
  if (s_cur == Screen::Module) refresh_module_view(now);
  if (s_dirty_status || now - s_last_status_ms >= kStatusRefreshMs) {
    ui::draw_status(current_mode(), strip_prefix(s_session, "sess-"), backend_short(), g_board.battery_percent(),
                    backend_conn());
    s_last_status_ms = now;
    s_dirty_status = false;
  }
  if (s_dirty_content) {
    s_dirty_content = false;
    render_content(now);
  }
  if (s_dirty_footer || (s_footer_timer_active && now - s_last_footer_ms >= kLiveRefreshMs)) {
    s_dirty_footer = false;
    s_last_footer_ms = now;
    render_footer(now);
  }
}

void on_enter(Screen s, bool returning) {
  if (!returning && (is_message_screen(s))) s_scroll = 0;
  if (s == Screen::RunView) s_answer_unseen = false;
  if (s == Screen::LedLegend && !returning) s_nav[sidx(Screen::LedLegend)] = ListNav{};
  switch (s) {
    case Screen::Security:
    case Screen::WifiSurvey:
    case Screen::BleExplorer:
    case Screen::Assess:
      refresh_counts();
      if (s == Screen::Assess) build_assess_items();
      break;
    case Screen::Targets:
      compute_sightings(true);
      break;
    case Screen::WifiResults:
    case Screen::BleResults:
    case Screen::GattTree:
      if (!returning) s_nav[sidx(s)] = ListNav{};
      break;
    default:
      break;
  }
}

// Global HOME. Boot, board-mismatch and USB-provisioning screens are not navigation routes. The confirm
// dialog keeps its key guard and is answered like Back (reject, never approve) before going home.
void go_home(uint32_t now) {
  if (s_cur == Screen::Boot || s_cur == Screen::BoardError || s_cur == Screen::Provisioning) return;
  if (s_cur == Screen::ConfirmDialog) {
    if (before(now, s_pc_guard_until)) return;  // keys typed before the dialog was readable
    if (s_pc_shown) resolve_pending(false);
  }
  if (s_cur == Screen::Module) modules::cancel(s_module);  // a module's confirmation is rejected, not left open
  nav_home();
}

// Global HELP: not on non-route screens, not over the modal confirm dialog (it shows its keys) and not in
// text entry (where '?' is a character and never reaches here).
bool help_allowed() {
  switch (s_cur) {
    case Screen::Boot:
    case Screen::BoardError:
    case Screen::Provisioning:
    case Screen::ConfirmDialog:
    case Screen::TextEntry:
      return false;
    case Screen::Module:
      return s_view.kind != modules::PageKind::Confirm;
    default:
      return true;
  }
}

// ---- lock screen (G0 button) ----------------------------------------------------------------------
// G0 covers whatever is open with the logo screen; any key uncovers it unchanged. It is a screen cover, not an
// authentication: operations, polling and the event queue keep running behind it, the emergency cancel chord
// still works, and the key that unlocks is consumed (it never acts on the screen underneath, nor on a
// confirmation dialog that opened meanwhile, whose key guard restarts).
bool s_locked = false;
bool s_swallow = false;  // after unlocking: keys are ignored until all are released (Cancel excepted)

bool lock_allowed() {
  return s_cur != Screen::Boot && s_cur != Screen::BoardError && s_cur != Screen::Provisioning;
}

void enter_lock() {
  s_locked = true;
  ui::draw_start_image();
  ui::draw_start_footer("Any key:Unlock", g_board.battery_percent());
}

void leave_lock(uint32_t now) {
  s_locked = false;
  s_swallow = true;
  if (s_cur == Screen::ConfirmDialog) s_pc_guard_until = now + kConfirmGuardMs;
  if (s_cur == Screen::Module && s_view.kind == modules::PageKind::Confirm) {
    s_module_guard_until = now + kConfirmGuardMs;
  }
  ui::clear_screen();
  mark_all();
}

}  // namespace

// ===== AppController ================================================================================

void AppController::set_startup_failures(uint8_t mask) { s_start_fail = static_cast<uint8_t>(s_start_fail | mask); }

void AppController::begin() {
  ui::begin();
  modules::begin();
  s_runtime = g_board.info().is_cardputer_adv;
  for (uint8_t i = 0; i < lab::kCharCount; ++i) s_lab_char_ok[i] = parse_uuid128(lab::kChars[i].uuid, s_lab_char_uuid[i]);
  s_lab_svc_ok = parse_uuid128(lab::kServiceUuid, s_lab_svc_uuid);
  build_assess_items();
  for (auto& s : s_seen) s = Seen::NoScan;
  g_board.set_brightness(kBrightness[s_brightness_idx]);

  QueueHandle_t q = ui_queue();
  if (q != nullptr) {
    telemetry_register_queue("ui_events", q, kUiQueueDepth, &s_ui_dropped);
  } else {
    s_start_fail = static_cast<uint8_t>(s_start_fail | kStartUiQueue);
  }

  load_config();
  s_cfg_gen = g_provisioning.config_generation();
  if (!open_session_now() && (s_start_fail & kStartEvidence) == 0) {
    s_start_fail = static_cast<uint8_t>(s_start_fail | kStartSession);
  }

  s_depth = 0;
  s_cur = s_runtime ? Screen::MainMenu : Screen::BoardError;
  on_enter(s_cur, false);
  // A failed module is announced on its own screen (Back returns to the menu, whose title and the status
  // bar keep naming it) and in Settings > Diagnostics.
  if (s_start_fail != 0 && s_runtime) show_detail(Screen::Message, "!Startup failure", build_startup_failure, nullptr);
  screen_ = s_cur;
  mark_all();
  draw(millis());
}

// The status LED mirrors the mode banner and the connection state (see status_led.h for the patterns).
// Attention (blue) wins over the connection state while something waits for the owner: a local confirmation
// (backend job or module page) or an AI answer that has not been opened yet.
bool attention_pending() {
  if (s_pc_shown) return true;
  if (s_cur == Screen::Module && s_view.kind == modules::PageKind::Confirm) return true;
  return s_answer_unseen;
}

statusled::Pattern led_pattern() {
  if (!s_runtime) return statusled::Pattern::Error;
  if (current_mode() == ModeBanner::LiveRadio) return statusled::Pattern::LiveRadio;
  if (attention_pending()) return statusled::Pattern::Attention;
  if (s_start_fail != 0) return statusled::Pattern::Error;
  switch (backend_conn()) {
    case ui::Conn::Online: return statusled::Pattern::Online;
    case ui::Conn::Connecting: return statusled::Pattern::Connecting;
    case ui::Conn::Error: return statusled::Pattern::Error;
    case ui::Conn::Offline:
    case ui::Conn::Unknown:
    default: return statusled::Pattern::Offline;
  }
}

void AppController::tick() {
  const uint32_t now = millis();
  statusled::set(led_pattern());
  statusled::tick(now);
  if (g_board.go_pressed() && !s_locked && lock_allowed()) enter_lock();
  bool keys_consumed = false;
  if (s_locked) {
    KeySnapshot k;
    if (g_board.read_keys(k)) {
      const NavOutput nav = s_input.map(k);
      if (nav.count > 0 && nav.events[0] == NavEvent::Cancel) {  // Cancel is always the first event
        if (s_cur == Screen::ConfirmDialog && s_pc_shown) resolve_pending(false);
        if (s_cur == Screen::Module) modules::cancel(s_module);
        emergency_cancel();
        ui::draw_start_footer("Cancelled", g_board.battery_percent());
      } else if (any_key(k)) {
        leave_lock(now);
        keys_consumed = true;
      }
    }
    if (s_locked) {
      on_event();
      if (now - s_last_poll_ms >= kPollMs) {
        s_last_poll_ms = now;
        poll_state(now);
      }
      ui::tick_start_screen();
      ui::refresh_start_footer(g_board.battery_percent());
      screen_ = s_cur;
      return;  // nothing of the covered screen is drawn
    }
  }
  s_input.set_text_mode(s_cur == Screen::TextEntry);
  if (s_swallow && !g_board.keys_down()) s_swallow = false;
  KeySnapshot k;
  if (!keys_consumed && g_board.read_keys(k)) {
    NavOutput nav = s_input.map(k);
    if (s_swallow && !(nav.count > 0 && nav.events[0] == NavEvent::Cancel)) nav.count = 0;
    if (s_cur == Screen::KeyTest && !s_swallow) keytest_record(k, nav);
    if (nav.count > 0) handle_input(nav);
  }
  on_event();
  if (now - s_last_poll_ms >= kPollMs) {
    s_last_poll_ms = now;
    poll_state(now);
  }
  if (s_cur == Screen::Module && modules::tick(s_module, now)) s_dirty_content = s_dirty_footer = true;
  const bool live = s_cur == Screen::Evidence || s_cur == Screen::RunView ||
                    (s_cur == Screen::Detail && s_detail_builder != nullptr) || progress_visible();
  if (live && now - s_last_live_ms >= kLiveRefreshMs) {
    s_last_live_ms = now;
    s_dirty_content = true;
  }
  draw(now);
  screen_ = s_cur;
}

void AppController::handle_input(const NavOutput& nav) {
  const uint8_t n = nav.count < 8 ? nav.count : 8;
  for (uint8_t i = 0; i < n; ++i) {
    const NavEvent e = nav.events[i];
    if (e == NavEvent::None) continue;
    if (e == NavEvent::Cancel) {
      if (s_cur == Screen::ConfirmDialog && s_pc_shown) resolve_pending(false);
      if (s_cur == Screen::Module) modules::cancel(s_module);
      emergency_cancel();
      // Acknowledge on screen in this same loop iteration (target: within cfg::kUiCancelAckMs).
      draw(millis());
      continue;
    }
    if (s_cur == Screen::KeyTest) {
      keytest_input(e, millis());
      continue;
    }
    if (e == NavEvent::Home) {
      go_home(millis());
      continue;
    }
    if (e == NavEvent::Help) {
      if (help_allowed()) open_help();
      continue;
    }
    dispatch(e, nav.chars[i]);
  }
  screen_ = s_cur;
}

void AppController::on_event() {
  QueueHandle_t q = ui_queue();
  if (q == nullptr) return;
  AppEvent ev;
  for (int budget = 16; budget > 0 && xQueueReceive(q, &ev, 0) == pdTRUE; --budget) {
    ev.text[sizeof(ev.text) - 1] = '\0';
    handle_event(ev);
  }
  screen_ = s_cur;
}

Screen AppController::screen() const { return s_cur; }

void AppController::go(Screen s) {
  nav_go(s);
  screen_ = s_cur;
}

}  // namespace oyster
