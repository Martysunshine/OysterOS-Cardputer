// Plain-C bridge between the generated Oyster PikaScript module (Oyster_Oyster.c, generated from
// ../../Oyster.pyi) and the real implementation in firmware/src/scripts_bridge.cpp, which is C++ and
// may touch Arduino/project headers that this library never includes. Keeping this boundary plain C
// means pikascript stays a self-contained vendored library; only Oyster_Oyster.c and this header are
// hand-written here.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// text is NUL-terminated, UTF-8, owned by the caller; the implementation copies what it keeps and never
// stores the pointer. Appends to the current script run's result log (bounded; oldest lines drop first).
void oyster_script_log(const char* text);

// Cooperative delay. Bounded to a documented maximum (scripts_bridge.cpp) regardless of what the script
// asked for, and returns early if the on-device cancel key was pressed - the caller (PikaVM) then sees
// pika_vm_exit() already requested and unwinds on its own next instruction check.
void oyster_script_sleep_ms(int ms);

// Access points seen in the last completed Wi-Fi survey, read-only; 0 if none has completed yet. Never
// starts a new survey.
int oyster_script_wifi_ap_count(void);

#ifdef __cplusplus
}
#endif
