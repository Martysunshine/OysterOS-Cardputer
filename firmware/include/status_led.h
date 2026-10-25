// Status LED: the single WS2812 on the StampS3 module shows the connection state at a glance, dim and slow
// (never above 100/255, nothing faster than a 1 s blink). Short announcements (an operation finished, a
// notice arrived) briefly override the state and then it resumes. No task or timer: the UI task calls
// tick() and the LED is written only when its colour changes. Implementation: src/status_led.cpp.
#pragma once

#include <cstdint>

namespace oyster::statusled {

enum class Pattern : uint8_t {
  Off,         // LED dark
  Boot,        // white, slow breathing (start screen before the first key)
  Online,      // green, slow breathing (backend reachable)
  Attention,   // blue, breathing: something waits for you (a confirmation, a new answer)
  Connecting,  // amber, breathing
  Offline,     // amber, one short blink every 2 s (not provisioned or backend unreachable)
  Error,       // red, one short blink every second (certificate, token or clock problem; start-up failure)
  LiveRadio,   // teal, breathing (a Wi-Fi survey or BLE operation is running)
  Count,
};

enum class Announce : uint8_t {
  Notice,   // blue, three quick blinks (a notice or request arrived)
  Success,  // green, two pulses (an operation finished)
  Failure,  // red, two longer blinks (an operation failed or was cut short)
};

void begin();                      // LED off; the driver is set up lazily on the first tick()
void set(Pattern p);               // the state shown when no announcement is playing; cheap
Pattern current();
void announce(Announce a);         // plays once over the state (about one second), then the state resumes
void tick(uint32_t now_ms);        // at most one write per 40 ms
// The colour a pattern uses at full strength (for the on-device legend), RGB 0..255 each.
void swatch(Pattern p, uint8_t& r, uint8_t& g, uint8_t& b);
void swatch(Announce a, uint8_t& r, uint8_t& g, uint8_t& b);

}  // namespace oyster::statusled
