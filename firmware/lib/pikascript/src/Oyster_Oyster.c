// Hand-written half of the Oyster script module (the other half, Oyster_Oyster.h and the dispatch in
// __pikaBinding.c, is generated from ../../Oyster.pyi by PikaPython's own compiler - see docs/SCRIPTS.md).
// Every method here is a thin, bounded call into firmware/src/scripts_bridge.cpp (plain C, so this file
// stays part of the vendored C library and never touches Arduino/C++ headers directly).
#include "Oyster_Oyster.h"

#include "oyster_script_bridge.h"

void Oyster_Oyster_log(PikaObj* self, char* text) {
  (void)self;
  oyster_script_log(text);
}

void Oyster_Oyster_sleep_ms(PikaObj* self, int ms) {
  (void)self;
  oyster_script_sleep_ms(ms);
}

int Oyster_Oyster_wifi_ap_count(PikaObj* self) {
  (void)self;
  return oyster_script_wifi_ap_count();
}
