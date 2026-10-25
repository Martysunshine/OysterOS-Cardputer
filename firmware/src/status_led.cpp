// Status LED on the WS2812 (GPIO21). Uses the Arduino core's neopixelWrite(): one RMT channel set up on the
// first write, then a blocking ~30 us transfer per write. Brightness curves are integer triangle waves so
// nothing allocates or uses floating point on the UI task.
#include "status_led.h"

#include <Arduino.h>

#include "oyster_pins.h"

namespace oyster::statusled {

namespace {

constexpr uint32_t kTickMs = 40;   // 25 updates per second at most
constexpr uint8_t kMax = 100;      // brightness ceiling (of 255), chosen on the real device
constexpr uint8_t kFloor = 8;      // breathing never goes fully dark

Pattern s_pattern = Pattern::Off;
uint32_t s_last_ms = 0;
uint8_t s_r = 0, s_g = 0, s_b = 0;
bool s_written = false;

// Triangle wave 0..255 over `period_ms`.
uint8_t triangle(uint32_t now, uint32_t period_ms) {
  const uint32_t t = now % period_ms;
  const uint32_t half = period_ms / 2;
  const uint32_t up = t < half ? t : period_ms - t;  // 0..half
  return static_cast<uint8_t>((up * 255u) / half);
}

// Breathing level: kFloor..kMax following the triangle wave.
uint8_t breathe(uint32_t now, uint32_t period_ms) {
  const uint32_t w = triangle(now, period_ms);
  return static_cast<uint8_t>(kFloor + ((kMax - kFloor) * w) / 255u);
}

// Blink level: kMax for `on_ms` at the start of every `period_ms`, else off.
uint8_t blink(uint32_t now, uint32_t period_ms, uint32_t on_ms) {
  return (now % period_ms) < on_ms ? kMax : 0;
}

void colour_for(Pattern p, uint32_t now, uint8_t& r, uint8_t& g, uint8_t& b) {
  r = g = b = 0;
  switch (p) {
    case Pattern::Off: break;
    case Pattern::Boot: { const uint8_t v = breathe(now, 2400); r = v; g = v; b = v; break; }
    case Pattern::Online: g = breathe(now, 3000); break;
    case Pattern::Connecting: { const uint8_t v = breathe(now, 1200); r = v; g = v / 2; break; }
    case Pattern::Offline: { const uint8_t v = blink(now, 2000, 120); r = v; g = v / 2; break; }
    case Pattern::Error: r = blink(now, 1000, 120); break;
    case Pattern::LiveRadio: { const uint8_t v = breathe(now, 900); g = v; b = v; break; }
  }
}

void write(uint8_t r, uint8_t g, uint8_t b) {
  if (s_written && r == s_r && g == s_g && b == s_b) return;
  neopixelWrite(static_cast<uint8_t>(pins::kRgbLed), r, g, b);
  s_r = r;
  s_g = g;
  s_b = b;
  s_written = true;
}

}  // namespace

void begin() {
  s_pattern = Pattern::Off;
  s_written = false;
  write(0, 0, 0);
}

void set(Pattern p) { s_pattern = p; }

Pattern current() { return s_pattern; }

void tick(uint32_t now_ms) {
  if (s_written && now_ms - s_last_ms < kTickMs) return;
  s_last_ms = now_ms;
  uint8_t r, g, b;
  colour_for(s_pattern, now_ms, r, g, b);
  write(r, g, b);
}

}  // namespace oyster::statusled
