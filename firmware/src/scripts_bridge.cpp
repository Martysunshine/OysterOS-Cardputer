// Plain-C surface the vendored PikaScript interpreter calls into (lib/pikascript/include/oyster_script_bridge.h),
// implemented here in C++ so it can use the project's own types. Everything here runs on the worker task, the
// same task that drives the interpreter (modules::Ops::call runs there), so the module's own view()/select()
// on the UI task only ever read what is written below, guarded by modules::Guard - never the other way round.
#include "oyster_script_bridge.h"

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "modules.h"
#include "oyster_core/types.h"
#include "results_cache.h"

extern "C" {
void pika_vm_exit(void);  // PikaVM.h; stops the interpreter at its next bytecode or loop check
}

namespace oyster::scripts {

namespace {
constexpr size_t kLogCap = 512;  // the Scripts module's Text page (PageView::body) shows at most this much
char s_log[kLogCap] = "";
size_t s_log_len = 0;
std::atomic<bool> s_cancel{false};
constexpr uint32_t kSleepChunkMs = 20;     // cancel is checked this often inside sleep_ms
constexpr uint32_t kSleepMaxMsPerCall = 5000;  // one call cannot block the worker longer than this
}  // namespace

// ---- called by firmware/src/modules/scripts.cpp (UI and worker task) --------------------------------

void reset_log() {
  modules::Guard g;
  s_log[0] = '\0';
  s_log_len = 0;
}

// Copies at most n-1 bytes of the current log into out (always NUL-terminated).
size_t read_log(char* out, size_t n) {
  modules::Guard g;
  if (out == nullptr || n == 0) return 0;
  const size_t len = s_log_len < n - 1 ? s_log_len : n - 1;
  std::memcpy(out, s_log, len);
  out[len] = '\0';
  return len;
}

void request_cancel() {
  s_cancel.store(true);
  pika_vm_exit();  // also unsticks a script in a tight loop, not just one asleep in sleep_ms
}

void clear_cancel() { s_cancel.store(false); }

bool cancel_requested() { return s_cancel.load(); }

}  // namespace oyster::scripts

// ---- called by the vendored interpreter (worker task only) ------------------------------------------

namespace {

void append_log(const char* text, size_t len) {
  using oyster::modules::Guard;
  Guard g;
  if (len == 0) return;
  // Bounded ring-free log: once full, the oldest lines drop (shift left) so the newest output is always
  // visible - a runaway print() loop fills the log with recent output, not a stale first line forever.
  if (len >= oyster::scripts::kLogCap) {
    text += len - (oyster::scripts::kLogCap - 1);
    len = oyster::scripts::kLogCap - 1;
  }
  if (oyster::scripts::s_log_len + len > oyster::scripts::kLogCap - 1) {
    const size_t keep = oyster::scripts::kLogCap - 1 - len;
    const size_t drop = oyster::scripts::s_log_len - keep;
    std::memmove(oyster::scripts::s_log, oyster::scripts::s_log + drop, keep);
    oyster::scripts::s_log_len = keep;
  }
  std::memcpy(oyster::scripts::s_log + oyster::scripts::s_log_len, text, len);
  oyster::scripts::s_log_len += len;
  oyster::scripts::s_log[oyster::scripts::s_log_len] = '\0';
}

}  // namespace

void oyster_script_log(const char* text) {
  if (text == nullptr) return;
  append_log(text, std::strlen(text));
}

void oyster_script_sleep_ms(int ms) {
  if (ms <= 0) return;
  const uint32_t cap = oyster::scripts::kSleepMaxMsPerCall;
  const uint32_t chunk = oyster::scripts::kSleepChunkMs;
  uint32_t remaining = static_cast<uint32_t>(ms) > cap ? cap : static_cast<uint32_t>(ms);
  while (remaining > 0 && !oyster::scripts::cancel_requested()) {
    const uint32_t step = remaining < chunk ? remaining : chunk;
    vTaskDelay(pdMS_TO_TICKS(step));  // same cooperative-delay pattern the radio operations already use
    remaining -= step;
  }
}

int oyster_script_wifi_ap_count(void) {
  oyster::WifiSummary ws{};
  oyster::ApObservation dummy{};
  oyster::g_results.wifi_rows(0, &dummy, 0, ws);
  return ws.count;
}

// pika_platform_printf (PikaPlatform.h, weak): captures print() and the interpreter's own error text into
// the same log as explicit Oyster.log() calls, instead of the USB-CDC serial port (which provisioning owns).
extern "C" void pika_platform_printf(char* fmt, ...) {
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  const int n = std::vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n > 0) append_log(buf, static_cast<size_t>(n) < sizeof(buf) ? static_cast<size_t>(n) : sizeof(buf) - 1);
}

// Fail-closed filesystem. Scripts run from a fixed, pre-loaded set; they have no business opening arbitrary
// files on the device. The vendored interpreter's weak defaults for these hooks spin forever on this target
// (PikaPlatform.h: WEAK_FUNCTION_NEED_OVERRIDE_ERROR_LOWLEVEL expands to `while (1) {}`), so a script that
// called open() would hang the worker task with no way for the cancel path to break in. These strong
// overrides make every file operation fail cleanly, so such a script gets an ordinary error it can handle
// instead of freezing the device. Reading the script source itself goes through LittleFS in scripts.cpp,
// not through these hooks, so nothing legitimate is lost.
extern "C" {
FILE* pika_platform_fopen(const char*, const char*) { return nullptr; }
int pika_platform_fclose(FILE*) { return -1; }
size_t pika_platform_fwrite(const void*, size_t, size_t, FILE*) { return 0; }
size_t pika_platform_fread(void*, size_t, size_t, FILE*) { return 0; }
int pika_platform_fseek(FILE*, long, int) { return -1; }
long pika_platform_ftell(FILE*) { return -1; }
int pika_platform_fflush(void*) { return 0; }
char* pika_platform_getcwd(char*, size_t) { return nullptr; }
int pika_platform_chdir(const char*) { return -1; }
int pika_platform_rmdir(const char*) { return -1; }
int pika_platform_mkdir(const char*, int) { return -1; }
int pika_platform_path_exists(const char*) { return 0; }
int pika_platform_path_isdir(const char*) { return 0; }
int pika_platform_path_isfile(const char*) { return 0; }
int pika_platform_remove(const char*) { return -1; }
int pika_platform_rename(const char*, const char*) { return -1; }
char** pika_platform_listdir(const char*, int* count) {
  if (count) *count = 0;
  return nullptr;
}
}  // extern "C"
