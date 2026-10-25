// Status LED on the WS2812 (GPIO21). Uses the Arduino core's neopixelWrite(): one RMT channel set up on the
// first write, then a blocking ~30 us transfer per write. Levels are integer triangle waves so nothing
// allocates or uses floating point on the UI task.
#include "status_led.h"

#include <Arduino.h>

#include "oyster_pins.h"

namespace oyster::statusled {

namespace {

constexpr uint32_t kTickMs = 40;   // 25 updates per second at most
constexpr uint8_t kMax = 100;      // brightness ceiling (of 255), chosen on the real device
constexpr uint8_t kFloor = 8;      // breathing never goes fully dark

struct Rgb {
  uint8_t r, g, b;
};

// Base colours at full strength (scaled by the level below).
constexpr Rgb kWhite{255, 255, 255};
constexpr Rgb kGreen{0, 255, 0};
constexpr Rgb kBlue{30, 90, 255};
constexpr Rgb kAmber{255, 110, 0};
constexpr Rgb kRed{255, 0, 0};
constexpr Rgb kTeal{0, 230, 200};
constexpr Rgb kBlack{0, 0, 0};

Pattern s_pattern = Pattern::Off;
Announce s_announce = Announce::Notice;
uint32_t s_announce_start = 0;
uint32_t s_announce_until = 0;
uint32_t s_last_ms = 0;
uint8_t s_r = 0, s_g = 0, s_b = 0;
bool s_written = false;

Rgb base(Pattern p) {
  switch (p) {
    case Pattern::Boot: return kWhite;
    case Pattern::Online: return kGreen;
    case Pattern::Attention: return kBlue;
    case Pattern::Connecting: return kAmber;
    case Pattern::Offline: return kAmber;
    case Pattern::Error: return kRed;
    case Pattern::LiveRadio: return kTeal;
    case Pattern::Off:
    case Pattern::Count:
    default: return kBlack;
  }
}

Rgb base(Announce a) {
  switch (a) {
    case Announce::Success: return kGreen;
    case Announce::Failure: return kRed;
    case Announce::Notice:
    default: return kBlue;
  }
}

// Triangle wave 0..255 over `period_ms`.
uint8_t triangle(uint32_t t, uint32_t period_ms) {
  const uint32_t x = t % period_ms;
  const uint32_t half = period_ms / 2;
  const uint32_t up = x < half ? x : period_ms - x;  // 0..half
  return static_cast<uint8_t>((up * 255u) / half);
}

// Breathing level kFloor..kMax following the triangle wave.
uint8_t breathe(uint32_t t, uint32_t period_ms) {
  const uint32_t w = triangle(t, period_ms);
  return static_cast<uint8_t>(kFloor + ((kMax - kFloor) * w) / 255u);
}

// Blink level: kMax for `on_ms` at the start of every `period_ms`, else off.
uint8_t blink(uint32_t t, uint32_t period_ms, uint32_t on_ms) { return (t % period_ms) < on_ms ? kMax : 0; }

// Level 0..kMax of a pattern at time `now`.
uint8_t level(Pattern p, uint32_t now) {
  switch (p) {
    case Pattern::Boot: return breathe(now, 2400);
    case Pattern::Online: return breathe(now, 3000);
    case Pattern::Attention: return breathe(now, 1400);
    case Pattern::Connecting: return breathe(now, 1200);
    case Pattern::Offline: return blink(now, 2000, 120);
    case Pattern::Error: return blink(now, 1000, 120);
    case Pattern::LiveRadio: return breathe(now, 900);
    case Pattern::Off:
    case Pattern::Count:
    default: return 0;
  }
}

// Level of an announcement `t` ms after it started; `len_ms` is its total length.
uint8_t level(Announce a, uint32_t t, uint32_t& len_ms) {
  switch (a) {
    case Announce::Notice: len_ms = 900; return blink(t, 300, 100);   // three quick blinks
    case Announce::Success: len_ms = 600; return blink(t, 300, 150);  // two pulses
    case Announce::Failure:
    default: len_ms = 1000; return blink(t, 500, 300);                // two longer blinks
  }
}

Rgb scaled(Rgb c, uint8_t lvl) {
  return Rgb{static_cast<uint8_t>((c.r * lvl) / 255u), static_cast<uint8_t>((c.g * lvl) / 255u),
             static_cast<uint8_t>((c.b * lvl) / 255u)};
}

void write(Rgb c) {
  if (s_written && c.r == s_r && c.g == s_g && c.b == s_b) return;
  neopixelWrite(static_cast<uint8_t>(pins::kRgbLed), c.r, c.g, c.b);
  s_r = c.r;
  s_g = c.g;
  s_b = c.b;
  s_written = true;
}

}  // namespace

void begin() {
  s_pattern = Pattern::Off;
  s_announce_until = 0;
  s_written = false;
  write(kBlack);
}

void set(Pattern p) { s_pattern = p; }

Pattern current() { return s_pattern; }

void announce(Announce a) {
  const uint32_t now = millis();
  uint32_t len = 0;
  (void)level(a, 0, len);
  s_announce = a;
  s_announce_start = now;
  s_announce_until = now + len;
}

void tick(uint32_t now_ms) {
  if (s_written && now_ms - s_last_ms < kTickMs) return;
  s_last_ms = now_ms;
  if (static_cast<int32_t>(s_announce_until - now_ms) > 0) {
    uint32_t len = 0;
    const uint8_t lvl = level(s_announce, now_ms - s_announce_start, len);
    write(scaled(base(s_announce), lvl));
    return;
  }
  write(scaled(base(s_pattern), level(s_pattern, now_ms)));
}

void swatch(Pattern p, uint8_t& r, uint8_t& g, uint8_t& b) {
  const Rgb c = base(p);
  r = c.r;
  g = c.g;
  b = c.b;
}

void swatch(Announce a, uint8_t& r, uint8_t& g, uint8_t& b) {
  const Rgb c = base(a);
  r = c.r;
  g = c.g;
  b = c.b;
}

}  // namespace oyster::statusled
