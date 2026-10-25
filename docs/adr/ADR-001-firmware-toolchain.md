# ADR-001: Firmware toolchain and build

**Status:** accepted (2026-10-24) · verify at M0 against `docs/BUILD_MATRIX.md`

**Decision.** PlatformIO with `platform = espressif32@6.13.0` (Arduino-ESP32 2.0.17 / ESP-IDF 4.4.7),
`board = esp32-s3-devkitc-1` (the recipe M5Stack documents for the Cardputer ADV), `flash_size = 8MB`,
a custom no-OTA partition table, and C++17. Pinned `lib_deps`: `m5stack/M5Cardputer@1.1.1`
+ `m5stack/M5Unified@0.2.27` + `m5stack/M5GFX@0.2.34` (all contain `board_M5CardputerADV`), `h2zero/NimBLE-Arduino@2.5.1`
(2.x API), `bblanchon/ArduinoJson@7.4.3`.

**Why.** 6.13.0 is the newest 6.x and the last on the 2.0.x core that M5 targets; Arduino core 3.x is
only on the community `pioarduino` fork, which M5 does not test. The USB-C port is the ESP32-S3 native
USB, so `ARDUINO_USB_CDC_ON_BOOT=1` and `ARDUINO_USB_MODE=1` are required for serial.

**Consequences.** No PSRAM (ESP32-S3FN8); RAM budgets in `oyster_core/config.h` apply. NimBLE is built
central+observer only, one connection, to save RAM/flash. Platform-independent logic lives in
`firmware/lib/oyster_core` so it also compiles for host unit tests (`env:native`).
