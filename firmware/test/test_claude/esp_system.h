// Host stand-in for the ESP-IDF header used by src/modules/deck.cpp (see Arduino.h in this folder).
#pragma once

#include <cstdint>

extern uint32_t g_rand;
inline uint32_t esp_random() {
  g_rand = g_rand * 1664525u + 1013904223u;
  return g_rand;
}
