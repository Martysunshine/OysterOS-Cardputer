// BoardSupport: M5 init, display, keyboard snapshot, battery, SD availability (board_support.h).
// Keyboard facts from the installed M5Cardputer 1.1.1 sources (utility/Keyboard/Keyboard.{h,cpp}):
//  - KeysState has tab, fn, shift, ctrl, opt, alt, del, enter, space and `word`; there are no arrow or
//    esc flags. Arrows are the Fn layer of ; . , / and Esc is the Fn layer of `.
//  - `del` is set by the key that emits KEY_BACKSPACE (the "del" keycap), so it maps to backspace.
//  - Under Ctrl or Shift `word` carries the second legend (Ctrl+C reports 'C'); Fn does not change it.
//  - isChange() only compares the number of held keys, so changes are detected here by comparing snapshots.
#include "board_support.h"

#include <Arduino.h>
#include <M5Cardputer.h>
#include <SD.h>
#include <SPI.h>
#include <esp_mac.h>
#include <esp_system.h>

#include <cstring>

#include "app_events.h"
#include "oyster_pins.h"

namespace oyster {

BoardSupport g_board;

namespace {

constexpr uint32_t kBatteryRefreshMs = 10000;
constexpr uint32_t kStorageRefreshMs = 30000;
constexpr uint32_t kRepeatDelayMs = 400;
constexpr uint32_t kRepeatPeriodMs = 90;

// Battery and storage values are cached so the UI loop never waits on the ADC or FAT free-space scan.
int s_battery_pct = -1;
uint32_t s_battery_ms = 0;

StorageInfo s_storage{SdState::NotPresent, 0, 0, 0};
uint32_t s_storage_ms = 0;
bool s_storage_valid = false;

KeySnapshot s_prev;               // full set held at the previous read
uint32_t s_held_since_ms = 0;     // when the current set was first seen
uint32_t s_last_repeat_ms = 0;

const char* reset_reason_name(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "other_wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_UNKNOWN:
    default: return "unknown";
  }
}

bool snap_has(const KeySnapshot& k, char c) {
  for (uint8_t i = 0; i < k.nchars && i < sizeof(k.chars); ++i) {
    if (k.chars[i] == c) return true;
  }
  return false;
}

bool same_keys(const KeySnapshot& a, const KeySnapshot& b) {
  if (a.up != b.up || a.down != b.down || a.left != b.left || a.right != b.right) return false;
  if (a.enter != b.enter || a.esc != b.esc || a.backspace != b.backspace || a.del != b.del) return false;
  if (a.tab != b.tab || a.space != b.space) return false;
  if (a.fn != b.fn || a.shift != b.shift || a.ctrl != b.ctrl || a.opt != b.opt || a.alt != b.alt) return false;
  if (a.nchars != b.nchars) return false;
  return std::memcmp(a.chars, b.chars, a.nchars) == 0;
}

// Current full key set from the library state (call after M5Cardputer.update()).
KeySnapshot current_keys() {
  const Keyboard_Class::KeysState& ks = M5Cardputer.Keyboard.keysState();
  KeySnapshot k;
  k.fn = ks.fn;
  k.shift = ks.shift;
  k.ctrl = ks.ctrl;
  k.opt = ks.opt;
  k.alt = ks.alt;
  k.enter = ks.enter;
  k.tab = ks.tab;
  k.space = ks.space;
  k.backspace = ks.del;
  for (char c : ks.word) {
    if (k.fn) {
      bool arrow = true;
      switch (c) {
        case ';': k.up = true; break;
        case '.': k.down = true; break;
        case ',': k.left = true; break;
        case '/': k.right = true; break;
        default: arrow = false; break;
      }
      if (arrow) continue;
    }
    if (k.nchars < sizeof(k.chars)) k.chars[k.nchars++] = c;
  }
  return k;
}

// Keys pressed in this change: action keys and characters that were not held before. When Fn or Ctrl
// was just pressed, keys already held count as new so chords typed key-first still register.
KeySnapshot pressed_edges(const KeySnapshot& cur, const KeySnapshot& prev) {
  KeySnapshot e;
  e.fn = cur.fn;
  e.shift = cur.shift;
  e.ctrl = cur.ctrl;
  e.opt = cur.opt;
  e.alt = cur.alt;
  const bool chord_started = (cur.fn && !prev.fn) || (cur.ctrl && !prev.ctrl);
  e.up = cur.up && (!prev.up || chord_started);
  e.down = cur.down && (!prev.down || chord_started);
  e.left = cur.left && (!prev.left || chord_started);
  e.right = cur.right && (!prev.right || chord_started);
  e.enter = cur.enter && !prev.enter;
  e.esc = cur.esc && !prev.esc;
  e.backspace = cur.backspace && !prev.backspace;
  e.del = cur.del && !prev.del;
  e.tab = cur.tab && !prev.tab;
  e.space = cur.space && !prev.space;
  for (uint8_t i = 0; i < cur.nchars && i < sizeof(cur.chars); ++i) {
    const char c = cur.chars[i];
    if (chord_started || !snap_has(prev, c)) e.chars[e.nchars++] = c;
  }
  return e;
}

// Auto-repeat only for the arrows and their bare legends. Backspace does not repeat because it is
// Back in navigation mode (a held key must not unwind several screens).
bool repeat_keys(const KeySnapshot& cur, KeySnapshot& out) {
  KeySnapshot r;
  r.fn = cur.fn;
  r.shift = cur.shift;
  r.ctrl = cur.ctrl;
  r.opt = cur.opt;
  r.alt = cur.alt;
  if (cur.ctrl || cur.opt || cur.alt) return false;
  r.up = cur.up;
  r.down = cur.down;
  r.left = cur.left;
  r.right = cur.right;
  for (uint8_t i = 0; i < cur.nchars && i < sizeof(cur.chars); ++i) {
    const char c = cur.chars[i];
    if (c == ';' || c == '.' || c == ',' || c == '/') r.chars[r.nchars++] = c;
  }
  if (!(r.up || r.down || r.left || r.right || r.nchars)) return false;
  out = r;
  return true;
}

void refresh_storage(SdState state) {
  s_storage.state = state;
  if (state == SdState::NotPresent || state == SdState::MountFailed) {
    s_storage.total_bytes = 0;
    s_storage.used_bytes = 0;
    s_storage.card_type = CARD_NONE;
  } else {
    s_storage.card_type = SD.cardType();
    s_storage.total_bytes = SD.totalBytes();
    s_storage.used_bytes = SD.usedBytes();
  }
  s_storage_ms = millis();
  s_storage_valid = true;
}

}  // namespace

const char* to_string(SdState s) {
  switch (s) {
    case SdState::NotPresent: return "not_present";
    case SdState::Mounted: return "mounted";
    case SdState::MountFailed: return "mount_failed";
    case SdState::WriteError: return "write_error";
    case SdState::Full: return "full";
  }
  return "unknown";
}

bool BoardSupport::begin() {
  auto cfg = M5.config();
  cfg.serial_baudrate = 0;   // Serial (HWCDC) is configured in main.cpp
  // The ES8311 microphone is used by the voice question (voice_capture.h). M5.begin() only configures the
  // pins and the private enable callback; no I2S or task runs until voice_capture calls M5.Mic.begin().
  cfg.internal_mic = true;
  cfg.internal_spk = false;
  cfg.internal_imu = false;
  cfg.internal_rtc = false;
  cfg.led_brightness = 0;
  M5Cardputer.begin(cfg, true);

  const m5::board_t board = M5.getBoard();
  info_.board_id = static_cast<int>(board);
  info_.is_cardputer_adv = (board == m5::board_t::board_M5CardputerADV);
  info_.flash_bytes = ESP.getFlashChipSize();
  info_.psram_found = psramFound();
  if (esp_read_mac(info_.mac, ESP_MAC_WIFI_STA) != ESP_OK) std::memset(info_.mac, 0, sizeof(info_.mac));
  const esp_reset_reason_t rr = esp_reset_reason();
  info_.reset_reason = static_cast<int>(rr);
  info_.reset_reason_str = reset_reason_name(rr);

  M5Cardputer.Display.setRotation(1);
  M5Cardputer.Display.fillScreen(0x0000);
  set_brightness(128);

  // SD on its own SPI bus (pins verified in oyster_pins.h). A missing card is a normal state.
  SPI.begin(pins::kSdSck, pins::kSdMiso, pins::kSdMosi, pins::kSdCs);
  remount_sd();

  s_battery_pct = static_cast<int>(M5.Power.getBatteryLevel());
  if (s_battery_pct < 0 || s_battery_pct > 100) s_battery_pct = -1;
  s_battery_ms = millis();
  return info_.is_cardputer_adv;
}

bool BoardSupport::remount_sd() {
  SD.end();
  SdState st = SdState::NotPresent;
  // SD.begin() fails both without a card and with an unreadable/unformatted one; the driver does not
  // tell them apart, so a failed begin is reported as not present.
  if (SD.begin(pins::kSdCs, SPI, pins::kSdFreqHz)) {
    st = (SD.cardType() == CARD_NONE) ? SdState::MountFailed : SdState::Mounted;
  }
  sd_state_ = st;
  refresh_storage(st);
  app_post(AppEventType::StorageChanged, static_cast<uint32_t>(st));
  return st == SdState::Mounted;
}

StorageInfo BoardSupport::storage() const {
  const SdState st = sd_state_;
  if (!s_storage_valid || st != s_storage.state || (st == SdState::Mounted && millis() - s_storage_ms > kStorageRefreshMs)) {
    refresh_storage(st);
  }
  return s_storage;
}

bool BoardSupport::sd_ready() const { return sd_state_ == SdState::Mounted; }

void BoardSupport::report_sd_write_error() {
  if (sd_state_ == SdState::WriteError) return;
  sd_state_ = SdState::WriteError;
  app_post(AppEventType::StorageChanged, static_cast<uint32_t>(SdState::WriteError));
}

void BoardSupport::report_sd_full() {
  if (sd_state_ == SdState::Full) return;
  sd_state_ = SdState::Full;
  app_post(AppEventType::StorageChanged, static_cast<uint32_t>(SdState::Full));
}

int BoardSupport::battery_percent() const {
  const uint32_t now = millis();
  if (now - s_battery_ms >= kBatteryRefreshMs) {
    const int32_t level = M5.Power.getBatteryLevel();
    s_battery_pct = (level < 0 || level > 100) ? -1 : static_cast<int>(level);
    s_battery_ms = now;
  }
  return s_battery_pct;
}

bool BoardSupport::read_keys(KeySnapshot& out) {
  const uint32_t now = millis();
  const KeySnapshot cur = current_keys();
  if (!same_keys(cur, s_prev)) {
    out = pressed_edges(cur, s_prev);
    s_prev = cur;
    s_held_since_ms = now;
    s_last_repeat_ms = now;
    return true;
  }
  if (now - s_held_since_ms >= kRepeatDelayMs && now - s_last_repeat_ms >= kRepeatPeriodMs) {
    s_last_repeat_ms = now;
    return repeat_keys(cur, out);
  }
  return false;
}

bool BoardSupport::go_pressed() { return M5.BtnA.wasPressed(); }

bool BoardSupport::keys_down() const { return any_key(current_keys()); }

void BoardSupport::set_brightness(uint8_t level) {
  // Raw backlight level 0..255, kept above zero so the screen never looks switched off.
  M5Cardputer.Display.setBrightness(level < 8 ? 8 : level);
}

}  // namespace oyster
