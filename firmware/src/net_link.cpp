// Wi-Fi STA link management. Called only from the worker task (BackendClient). WiFi.persistent(false)
// keeps the passphrase out of the Wi-Fi driver's own NVS namespace.
#include "net_link.h"

#include <Arduino.h>
#include <WiFi.h>

#include <cstring>

#include "device_config.h"
#include "time_sync.h"

namespace oyster::net {

namespace {

constexpr uint32_t kAttemptTimeoutMs = 15000;  // one association attempt
constexpr uint32_t kBackoffMinMs = 5000;
constexpr uint32_t kBackoffMaxMs = 60000;

bool s_attempting = false;
uint32_t s_attempt_start_ms = 0;
uint32_t s_next_try_ms = 0;
bool s_backoff_active = false;
uint32_t s_backoff_ms = 0;
uint32_t s_attempts = 0;

void secure_zero(void* p, size_t n) {
  volatile uint8_t* v = static_cast<volatile uint8_t*>(p);
  while (n--) *v++ = 0;
}

bool due(uint32_t now) { return !s_backoff_active || static_cast<int32_t>(now - s_next_try_ms) >= 0; }

void fail_attempt(uint32_t now) {
  s_attempting = false;
  s_backoff_ms = s_backoff_ms == 0 ? kBackoffMinMs : (s_backoff_ms * 2 > kBackoffMaxMs ? kBackoffMaxMs : s_backoff_ms * 2);
  s_next_try_ms = now + s_backoff_ms;
  s_backoff_active = true;
  log_w("wifi: association failed, retry in %lu ms", static_cast<unsigned long>(s_backoff_ms));
}

void on_link_up() {
  s_attempting = false;
  s_backoff_active = false;
  s_backoff_ms = 0;
  timesync::start_sntp();
}

bool start_attempt() {
  DeviceConfig c{};
  if (!g_config.load(c) || c.wifi_ssid[0] == '\0') return false;
  char pw[72];
  const bool have_pw = g_config.get_wifi_password(pw, sizeof(pw));
  WiFi.persistent(false);
  const wifi_mode_t mode = WiFi.getMode();
  if (mode != WIFI_STA && mode != WIFI_AP_STA) WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(c.wifi_ssid, (have_pw && pw[0] != '\0') ? pw : nullptr);
  secure_zero(pw, sizeof(pw));
  ++s_attempts;
  s_attempting = true;
  s_attempt_start_ms = millis();
  return true;
}

}  // namespace

bool configured() {
  DeviceConfig c{};
  return g_config.load(c) && c.wifi_ssid[0] != '\0';
}

bool up() { return WiFi.status() == WL_CONNECTED; }

bool ensure_up(uint32_t wait_ms) {
  if (up()) {
    if (s_attempting || s_backoff_active) on_link_up();
    timesync::start_sntp();
    return true;
  }
  uint32_t now = millis();
  if (!s_attempting) {
    if (!due(now)) return false;
    if (!start_attempt()) return false;
  }
  const uint32_t start = millis();
  while (!up() && millis() - start < wait_ms) vTaskDelay(pdMS_TO_TICKS(100));
  if (up()) {
    on_link_up();
    return true;
  }
  now = millis();
  if (now - s_attempt_start_ms >= kAttemptTimeoutMs) {
    WiFi.disconnect(false, false);
    fail_attempt(now);
  }
  return false;
}

int8_t rssi() { return up() ? static_cast<int8_t>(WiFi.RSSI()) : 0; }

uint32_t connect_attempts() { return s_attempts; }

}  // namespace oyster::net
