// Module framework: dispatch from the UI to the per-module tables, the shared lock and the worker hand-off.
// The modules themselves are in src/modules/<name>.cpp. See include/modules.h for the contract.
#include "modules.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstdio>
#include <cstring>

#include "app_events.h"
#include "assessment_runner.h"
#include "backend_client.h"
#include "backend_internal.h"

namespace oyster::modules {

namespace {
SemaphoreHandle_t s_mutex = nullptr;

const Ops* table(Id id) {
  switch (id) {
    case Id::Hosts: return &kHostsOps;
    case Id::Deck: return &kDeckOps;
    case Id::SmartHome: return &kSmartHomeOps;
    case Id::Original: return &kOriginalOps;
    case Id::Count: break;
  }
  return nullptr;
}
}  // namespace

const Ops& ops(Id id) {
  const Ops* t = table(id);
  return t != nullptr ? *t : kOriginalOps;  // never null: Id::Count is not a valid argument
}

const char* name(Id id) { return ops(id).name; }

void begin() {
  if (s_mutex == nullptr) s_mutex = xSemaphoreCreateRecursiveMutex();
}

void lock() {
  if (s_mutex != nullptr) xSemaphoreTakeRecursive(s_mutex, portMAX_DELAY);
}

void unlock() {
  if (s_mutex != nullptr) xSemaphoreGiveRecursive(s_mutex);
}

bool request(Id id, uint8_t call_id, const char* arg) {
  if (static_cast<uint8_t>(id) >= kCount) return false;
  WorkRequest r;
  std::memset(&r, 0, sizeof(r));
  r.kind = RequestKind::Module;
  r.module_id = static_cast<uint8_t>(id);
  r.call_id = call_id;
  if (arg != nullptr) std::snprintf(r.arg, sizeof(r.arg), "%s", arg);
  return g_runner.enqueue(r);
}

void changed() { app_post(AppEventType::ModuleChanged); }

void toast(const char* text) { app_post(AppEventType::Toast, 0, 0, text); }

bool backend_ready() { return g_backend.state() == BackendState::Connected; }

int http(const char* method, const char* path, const char* body, size_t body_len, char* resp, size_t cap,
         size_t* resp_len, bool idempotent) {
  return g_backend.call(method, path, body, body_len, resp, cap, resp_len, idempotent);
}

const char* last_error() { return backend_detail::last_error(); }

// ---- UI-side dispatch ----------------------------------------------------------------------------------------

void open(Id id) {
  const Ops& o = ops(id);
  if (o.open != nullptr) o.open();
}

void view(Id id, PageView& v) {
  const Ops& o = ops(id);
  v = PageView{};
  if (o.view != nullptr) o.view(v);
  v.title[sizeof(v.title) - 1] = '\0';
  v.body[sizeof(v.body) - 1] = '\0';
  v.left[sizeof(v.left) - 1] = '\0';
  v.right[sizeof(v.right) - 1] = '\0';
}

void select(Id id, uint16_t row) {
  const Ops& o = ops(id);
  if (o.select != nullptr) o.select(row);
}

bool back(Id id) {
  const Ops& o = ops(id);
  return o.back != nullptr && o.back();
}

bool key(Id id, NavEvent e, char c) {
  const Ops& o = ops(id);
  return o.key != nullptr && o.key(e, c);
}

bool tick(Id id, uint32_t now_ms) {
  const Ops& o = ops(id);
  return o.tick != nullptr && o.tick(now_ms);
}

void cancel(Id id) {
  const Ops& o = ops(id);
  if (o.cancel != nullptr) o.cancel();
}

void tile_status(Id id, char* out, size_t n) {
  if (n == 0) return;
  out[0] = '\0';
  const Ops& o = ops(id);
  if (o.tile_status != nullptr) o.tile_status(out, n);
  out[n - 1] = '\0';
}

// ---- worker-side -------------------------------------------------------------------------------------------------

void run_call(uint8_t module_id, uint8_t call_id, const char* arg) {
  if (module_id >= kCount) return;
  const Ops& o = ops(static_cast<Id>(module_id));
  if (o.call != nullptr) o.call(call_id, arg != nullptr ? arg : "");
}

}  // namespace oyster::modules
