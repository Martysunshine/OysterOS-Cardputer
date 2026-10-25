// Status LED: the single WS2812 on the StampS3 module shows the connection state at a glance, dim and slow
// (never above 40/255, nothing faster than a 1 s blink). No task or timer: the UI task calls tick() and the
// LED is written only when its colour changes. Implementation: src/status_led.cpp.
#pragma once

#include <cstdint>

namespace oyster::statusled {

enum class Pattern : uint8_t {
  Off,         // LED dark
  Boot,        // white, slow breathing (start screen before the first key)
  Online,      // green, slow breathing (backend reachable)
  Connecting,  // amber, breathing
  Offline,     // amber, one short blink every 2 s (not provisioned or backend unreachable)
  Error,       // red, one short blink every second (certificate, token or clock problem; start-up failure)
  LiveRadio,   // teal, breathing (a Wi-Fi survey or BLE operation is running)
};

void begin();                 // LED off; first call initialises the driver lazily on tick()
void set(Pattern p);          // cheap; takes effect on the next tick
Pattern current();
void tick(uint32_t now_ms);   // at most one write per 40 ms

}  // namespace oyster::statusled
