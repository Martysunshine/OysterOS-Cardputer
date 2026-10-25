// Worker-task -> UI events. Implementation: app_controller.cpp. Never posted from radio callbacks.
#pragma once

#include <cstdint>

namespace oyster {

enum class AppEventType : uint8_t {
  OperationStarted,       // a = Operation, text = summary
  OperationProgress,      // a = Operation, b = items so far
  OperationFinished,      // a = Operation, b = EventStatus, text = summary
  ResultsUpdated,         // a = ResultKind; UI re-reads shared results
  BackendStatusChanged,   // a = BackendState
  ConfirmationRequested,  // details via g_runner.pending_confirmation()
  ConfirmationClosed,     // a: 1 approved, 0 rejected, 2 expired
  RunUpdated,             // AI run changed; UI re-reads latest run
  StorageChanged,         // a = SdState
  Toast,                  // text
};

enum class ResultKind : uint8_t { Wifi, Ble, Gatt, Read };

struct AppEvent {
  AppEventType type;
  uint32_t a;
  uint32_t b;
  char text[64];
};

// Non-blocking, thread-safe. False when the UI queue is full (event dropped and counted).
bool app_post_event(const AppEvent& ev);
bool app_post(AppEventType type, uint32_t a = 0, uint32_t b = 0, const char* text = nullptr);

}  // namespace oyster
