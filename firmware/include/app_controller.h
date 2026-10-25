// AppController: screen state machine, session lifecycle, cancel, user-visible errors, mode banner.
// Runs in the Arduino loop task; drains the UI event queue and redraws with small sprites.
// Navigation follows the OysterOS contract (docs/UI_SCREEN_MAP.md maps design screen IDs to Screen):
// a bounded stack, Back pops (a confirm dialog is answered "reject" first), Home clears the stack to
// MainMenu, Help opens the bindings for the current screen. Cancel works on every screen.
// Mode banner (most specific first): LIVE RADIO while a radio phase is active, START FAILED when a module
// did not start, AI CONNECTED only while the latest AI run used a provider, BACKEND OK when the backend is
// reachable and authenticated, else OFFLINE. SIMULATED is never shown for firmware data.
// Implementation: app_controller.cpp.
#pragma once

#include <cstdint>

#include "oyster_core/input_map.h"

namespace oyster {

enum class Screen : uint8_t {
  Boot, Provisioning, BoardError, MainMenu,
  Security, WifiSurvey, BleExplorer, Targets, Assess, AskOyster, Evidence, Settings,
  WifiResults, BleResults, GattTree, Detail,
  RunView, ConfirmDialog, TextEntry, Message, Module,
  Unavailable, Help, KeyTest,  // keep KeyTest last (kScreenCount in app_controller.cpp)
};

enum class ModeBanner : uint8_t { Offline, LiveRadio, AiConnected, Simulated, BackendOk, StartFailed };

// Modules that did not start at boot (bit set = failed). main.cpp reports them before begin(); the UI keeps
// a banner, lists them in Settings > Diagnostics and refuses the actions that depend on them.
enum StartupFailure : uint8_t {
  kStartConfig = 1u << 0,    // g_config.begin(): NVS namespace unavailable
  kStartEvidence = 1u << 1,  // g_evidence.begin()
  kStartBle = 1u << 2,       // g_ble.begin()
  kStartBackend = 1u << 3,   // g_backend.begin()
  kStartRunner = 1u << 4,    // g_runner.begin(): worker task or request queue
  kStartUiQueue = 1u << 5,   // worker -> UI event queue (set by begin() itself)
  kStartSession = 1u << 6,   // no evidence session could be opened at boot (cleared by Settings > New session)
};

class AppController {
 public:
  void set_startup_failures(uint8_t mask);  // before begin()
  void begin();
  void tick();                 // once per loop: input -> state -> draw (partial)
  void handle_input(const NavOutput& nav);
  void on_event();             // drain the UI queue
  Screen screen() const;
  void go(Screen s);

 private:
  Screen screen_ = Screen::Boot;
};

extern AppController g_app;

}  // namespace oyster
