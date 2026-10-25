// BoardSupport: M5 init, display, keyboard snapshot, battery, SD availability. Implementation: board_support.cpp.
// Boot must report m5::board_t::board_M5CardputerADV; otherwise the keyboard driver is wrong and the
// UI shows a board-mismatch error.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/input_map.h"

namespace oyster {

struct BoardInfo {
  int board_id;              // M5.getBoard()
  bool is_cardputer_adv;
  uint32_t flash_bytes;      // ESP.getFlashChipSize()
  bool psram_found;          // expected false on the ADV
  uint8_t mac[6];            // Wi-Fi STA MAC
  int reset_reason;          // esp_reset_reason()
  const char* reset_reason_str;
};

enum class SdState : uint8_t { NotPresent, Mounted, MountFailed, WriteError, Full };
const char* to_string(SdState s);

struct StorageInfo {
  SdState state;
  uint64_t total_bytes;
  uint64_t used_bytes;
  uint8_t card_type;  // SD.cardType()
};

class BoardSupport {
 public:
  bool begin();                      // M5Cardputer.begin(cfg, true); display; SD via oyster_pins.h
  const BoardInfo& info() const { return info_; }
  StorageInfo storage() const;
  bool sd_ready() const;
  void report_sd_write_error();
  void report_sd_full();
  bool remount_sd();
  int battery_percent() const;       // -1 when unknown
  bool read_keys(KeySnapshot& out);  // after M5Cardputer.update(); true on change. Arrows/esc derived from Fn combos
  bool go_pressed();                 // G0 button (GPIO0) pressed since the last call; after M5Cardputer.update()
  bool keys_down() const;            // any keyboard key or modifier is held right now
  void set_brightness(uint8_t level);  // raw backlight 0-255, clamped to >= 8 so the screen never goes dark

 private:
  BoardInfo info_{};
  volatile SdState sd_state_ = SdState::NotPresent;
};

extern BoardSupport g_board;

}  // namespace oyster
