// Cardputer ADV pin map (docs.m5stack.com/en/core/Cardputer-Adv, checked 2026-10-24; LED and IR pins
// cross-checked with the Cardputer board support package on 2026-10-26).
// Display/keyboard/audio are driven by M5Cardputer/M5Unified; these constants are for the SD bus, the
// status LED and diagnostics only. Do not reuse pins from original-Cardputer projects.
#pragma once

#include <cstdint>

namespace oyster::pins {

constexpr int kSdSck = 40;
constexpr int kSdMiso = 39;
constexpr int kSdMosi = 14;
constexpr int kSdCs = 12;
constexpr uint32_t kSdFreqHz = 25000000;  // SPI.begin(40, 39, 14, 12); SD.begin(12, SPI, 25000000)

constexpr int kI2cSda = 8;        // shared: TCA8418 keyboard, ES8311 codec, BMI270 IMU
constexpr int kI2cScl = 9;
constexpr int kKeyboardInt = 11;
constexpr int kRgbLed = 21;       // WS2812 status LED on the StampS3 module (one pixel, data only)
constexpr int kIrTx = 44;         // IR transmitter LED (used by src/modules/ir_remote.cpp)

}  // namespace oyster::pins
