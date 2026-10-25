// Host stand-in for the Arduino core: just enough for src/modules/deck.cpp to compile in a native test.
// It lives in the test folder only (PlatformIO adds this folder to the include path of this suite alone).
#pragma once

#include <cstdint>

extern uint32_t g_now_ms;  // the test moves the clock by hand
inline uint32_t millis() { return g_now_ms; }
