// C++ side of firmware/src/scripts_bridge.cpp: what src/modules/scripts.cpp calls to reset and read the
// script run log and to cancel a running script. The interpreter itself calls the plain-C surface in
// lib/pikascript/include/oyster_script_bridge.h, implemented in the same .cpp file.
#pragma once

#include <cstddef>

namespace oyster::scripts {

void reset_log();
size_t read_log(char* out, size_t n);  // NUL-terminated copy of the current log, at most n-1 bytes

void request_cancel();  // from the module's cancel() (UI task): stops the VM and any sleep_ms in progress
void clear_cancel();     // before starting a new run
bool cancel_requested();

}  // namespace oyster::scripts
