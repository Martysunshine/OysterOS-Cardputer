// Oyster Pocket firmware entry point: boot order (see docs/ARCHITECTURE.md), boot evidence event, loop.
// On a board other than the Cardputer ADV only config + evidence start (to record the mismatch) and the
// UI shows the board-mismatch screen; radio, BLE, backend and runner stay off.
#include <Arduino.h>
#include <M5Cardputer.h>
#include <WiFi.h>
#include <esp_idf_version.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <cstdio>
#include <memory>
#include <new>

#include "app_controller.h"
#include "assessment_runner.h"
#include "backend_client.h"
#include "ble_explorer.h"
#include "board_support.h"
#include "device_config.h"
#include "evidence_store.h"
#include "oyster_core/json_writer.h"
#include "radio_scheduler.h"
#include "results_cache.h"
#include "serial_provisioning.h"
#include "telemetry.h"
#include "ui.h"
#include "assets.h"

#ifndef OYSTER_FIRMWARE_VERSION
#define OYSTER_FIRMWARE_VERSION "0.0.0-dev"
#endif

using namespace oyster;

namespace {

constexpr uint32_t kLoopDelayMs = 10;  // ~100 Hz input polling; lets lower-priority tasks run

struct BootResults {
  bool ui = false;
  bool config = false;
  bool evidence = false;
  bool ble = false;
  bool backend = false;
  bool runner = false;
  bool runtime_started = false;
};

BootResults s_boot;

// Boot splash: the image is drawn once; start-up steps then only update the footer line and advance the
// mascot's loader animation by at most one frame per frame period. No task or timer: frames move only
// while setup() is running, and the animation stops when the first screen replaces the splash.
uint8_t s_loader_frame = 0;
uint32_t s_loader_ms = 0;
constexpr uint32_t kMinSplashMs = 1500;  // keep the splash up long enough to be seen on a fast start

void loader_tick() {
  const assets::Animation& a = assets::loader();
  const uint32_t period = a.fps > 0 ? 1000u / a.fps : 200u;
  const uint32_t now = millis();
  if (now - s_loader_ms < period) return;
  s_loader_ms = now;
  ui::draw_animation_frame(a, s_loader_frame);
  s_loader_frame = static_cast<uint8_t>((s_loader_frame + 1) % (a.frames > 0 ? a.frames : 1));
}

void splash(const char* step) {
  if (!s_boot.ui) return;
  char left[40];
  std::snprintf(left, sizeof(left), "Starting: %s", step);
  ui::draw_footer(left, "v" OYSTER_FIRMWARE_VERSION);
  loader_tick();
}

void write_boot_event() {
  // Scratch is needed once, so it is borrowed from the heap instead of staying resident.
  std::unique_ptr<char[]> scratch(new (std::nothrow) char[cfg::kMaxEventBytes]);
  if (!scratch) {
    Serial.printf("[oyster] boot event not recorded: out of memory\n");
    return;
  }
  const BoardInfo& bi = g_board.info();
  EventHeader h{};
  h.op = Operation::Boot;
  h.transport = Transport::Local;
  h.status = bi.is_cardputer_adv ? EventStatus::Success : EventStatus::Error;
  h.source_mode = SourceMode::Live;
  h.target_id = nullptr;
  JsonWriter& w = g_evidence.begin_event(h, scratch.get(), cfg::kMaxEventBytes);
  w.str("firmware_version", OYSTER_FIRMWARE_VERSION);
  w.num("board_id", bi.board_id);
  w.boolean("is_cardputer_adv", bi.is_cardputer_adv);
  w.str("board_check", bi.is_cardputer_adv ? "ok" : "board_mismatch");
  w.unum("flash_bytes", bi.flash_bytes);
  w.boolean("psram_found", bi.psram_found);
  char mac[18];
  format_mac(bi.mac, mac, sizeof(mac));
  w.str("wifi_sta_mac", mac);
  w.str("reset_reason", bi.reset_reason_str);
  w.num("reset_reason_code", bi.reset_reason);
  w.str("idf_version", esp_get_idf_version());
  w.unum("cpu_mhz", getCpuFrequencyMhz());
  const StorageInfo st = g_board.storage();
  w.str("sd_state", to_string(st.state));
  w.boolean("evidence_sd_backed", g_evidence.sd_backed());
  w.str("keyboard_map", "proposed_unverified");
  w.begin_object("init");
  w.boolean("ui", s_boot.ui);
  w.boolean("config", s_boot.config);
  w.boolean("evidence", s_boot.evidence);
  w.boolean("runtime_started", s_boot.runtime_started);
  if (s_boot.runtime_started) {
    w.boolean("ble", s_boot.ble);
    w.boolean("backend", s_boot.backend);
    w.boolean("runner", s_boot.runner);
  }
  w.end_object();
  TelemetrySnapshot t;
  telemetry_snapshot(t);
  w.begin_object("telemetry");
  telemetry_write_json(t, w);
  w.end_object();
  char id[cfg::kIdBufLen] = {};
  const bool ok = g_evidence.commit_event(id);
  Serial.printf("[oyster] boot event %s\n", ok ? id : "not recorded");
}

// Tells the UI which modules did not start, so it can say so and refuse the actions that depend on them.
void report_startup_failures() {
  uint8_t mask = 0;
  if (!s_boot.config) mask |= kStartConfig;
  if (!s_boot.evidence) mask |= kStartEvidence;
  if (s_boot.runtime_started) {
    if (!s_boot.ble) mask |= kStartBle;
    if (!s_boot.backend) mask |= kStartBackend;
    if (!s_boot.runner) mask |= kStartRunner;
  }
  if (mask != 0) Serial.printf("[oyster] startup failures (mask 0x%02x): see Settings > Diagnostics\n", mask);
  g_app.set_startup_failures(mask);
}

}  // namespace

void setup() {
  // Before anything can start the Wi-Fi driver (a survey or the uplink): the driver reads this flag only
  // when it first initialises, and the default would store the station passphrase in its own NVS namespace.
  WiFi.persistent(false);

  // USB CDC (HWCDC). Writes are bounded: with no host draining the port the core drops data instead of
  // blocking. A short non-zero timeout is used because HWCDC::write() in core 2.0.17 uses the timeout as
  // its no-progress retry budget, and 0 would underflow it while a host is attached but not reading.
  Serial.setRxBufferSize(1024);  // provisioning lines are at most 300 bytes
  Serial.setTxTimeoutMs(5);
  Serial.begin(115200);

  g_board.begin();
  ui::begin();
  s_boot.ui = ui::ready();
  if (!s_boot.ui) Serial.printf("[oyster] display sprites unavailable (out of memory): screen output disabled\n");
  // Boot splash: drawn once, in bands (no full-screen buffer); the first screen replaces it.
  const uint32_t splash_start = millis();
  if (s_boot.ui) {
    ui::draw_image(assets::splash(), 0, 0, 0);
    splash("display");
  }
  telemetry_register_task("loop", xTaskGetCurrentTaskHandle());

  splash("config");
  s_boot.config = g_config.begin();
  splash("evidence");
  s_boot.evidence = g_evidence.begin(SourceMode::Live);

  if (g_board.info().is_cardputer_adv) {
    splash("radio");
    g_radio.begin();
    g_results.begin();
    splash("BLE");
    s_boot.ble = g_ble.begin();
    splash("backend");
    s_boot.backend = g_backend.begin();
    splash("runner");
    s_boot.runner = g_runner.begin();
    s_boot.runtime_started = true;
  }

  report_startup_failures();
  while (s_boot.ui && millis() - splash_start < kMinSplashMs) {  // let a fast start still show the animation
    loader_tick();
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  g_app.begin();  // opens the session (needed before the boot event can be recorded)
  write_boot_event();
}

void loop() {
  M5Cardputer.update();
  g_provisioning.poll();
  g_app.tick();
  vTaskDelay(pdMS_TO_TICKS(kLoopDelayMs));
}
