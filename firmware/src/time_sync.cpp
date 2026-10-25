// Clock state: quality is tracked separately from the system time. The quality survives a software
// reset (provisioning DONE reboots) through an RTC no-init marker, and only while the clock itself
// is still plausible.
#include "time_sync.h"

#include <Arduino.h>
#include <esp_attr.h>
#include <esp_sntp.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <sys/time.h>
#include <time.h>

#include <atomic>
#include <cstdio>

namespace oyster::timesync {

namespace {

constexpr uint32_t kMarkMagic = 0x4F59434Bu;  // "OYCK"

struct RtcClockMark {
  uint32_t magic;
  uint32_t quality;
  uint32_t check;
};

RTC_NOINIT_ATTR RtcClockMark s_mark;

std::atomic<uint8_t> s_quality{static_cast<uint8_t>(ClockQuality::Unknown)};
std::atomic<bool> s_init{false};
std::atomic<bool> s_sntp_started{false};
std::atomic<bool> s_ntp_synced{false};
volatile uint64_t s_floor = kMinPlausibleUnix;  // written once at boot by DeviceConfigStore::begin

uint64_t raw_now() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return tv.tv_sec < 0 ? 0 : static_cast<uint64_t>(tv.tv_sec);
}

uint64_t floor_value() {
  const uint64_t f = s_floor;
  return f > kMinPlausibleUnix ? f : kMinPlausibleUnix;
}

bool in_range(uint64_t t) { return t >= floor_value() && t < kMaxPlausibleUnix; }

void write_mark(ClockQuality q) {
  s_mark.magic = kMarkMagic;
  s_mark.quality = static_cast<uint32_t>(q);
  s_mark.check = ~(kMarkMagic ^ static_cast<uint32_t>(q));
}

void init_once() {
  bool expected = false;
  if (!s_init.compare_exchange_strong(expected, true)) return;
  const esp_reset_reason_t rr = esp_reset_reason();
  const bool warm = rr != ESP_RST_POWERON && rr != ESP_RST_BROWNOUT && rr != ESP_RST_UNKNOWN;
  const bool mark_ok = s_mark.magic == kMarkMagic && s_mark.check == ~(kMarkMagic ^ s_mark.quality) &&
                       s_mark.quality <= static_cast<uint32_t>(ClockQuality::BackendHint);
  if (warm && mark_ok && in_range(raw_now())) {
    s_quality.store(static_cast<uint8_t>(s_mark.quality));
  } else {
    s_mark.magic = 0;
  }
}

void on_sntp_sync(struct timeval*) {
  // Runs in the lwIP task: only atomics and the RTC marker.
  s_quality.store(static_cast<uint8_t>(ClockQuality::Ntp));
  s_ntp_synced.store(true);
  write_mark(ClockQuality::Ntp);
}

bool set_system_time(uint64_t unix_seconds) {
  struct timeval tv;
  tv.tv_sec = static_cast<time_t>(unix_seconds);
  tv.tv_usec = 0;
  return settimeofday(&tv, nullptr) == 0;
}

}  // namespace

void set_floor(uint64_t last_known_good_unix) {
  if (last_known_good_unix >= kMinPlausibleUnix && last_known_good_unix < kMaxPlausibleUnix) {
    s_floor = last_known_good_unix;
  }
}

bool plausible() { return in_range(raw_now()); }

ClockQuality quality() {
  init_once();
  if (!plausible()) return ClockQuality::Unknown;
  return static_cast<ClockQuality>(s_quality.load());
}

bool now_unix(uint64_t& out) {
  const uint64_t t = raw_now();
  if (!in_range(t)) return false;
  out = t;
  return true;
}

ClockQuality format_utc(char* out, size_t n) {
  if (out == nullptr || n == 0) return ClockQuality::Unknown;
  out[0] = '\0';
  const ClockQuality q = quality();
  if (q == ClockQuality::Unknown || n < 25) return ClockQuality::Unknown;
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  const time_t secs = tv.tv_sec;
  struct tm tm_utc;
  if (gmtime_r(&secs, &tm_utc) == nullptr) return ClockQuality::Unknown;
  const int ms = static_cast<int>(tv.tv_usec / 1000);
  const int w = snprintf(out, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm_utc.tm_year + 1900, tm_utc.tm_mon + 1,
                         tm_utc.tm_mday, tm_utc.tm_hour, tm_utc.tm_min, tm_utc.tm_sec, ms);
  if (w <= 0 || static_cast<size_t>(w) >= n) {
    out[0] = '\0';
    return ClockQuality::Unknown;
  }
  return q;
}

bool set_from_serial(uint64_t unix_seconds) {
  init_once();
  if (unix_seconds < kMinPlausibleUnix || unix_seconds >= kMaxPlausibleUnix) return false;
  if (!set_system_time(unix_seconds)) return false;
  s_quality.store(static_cast<uint8_t>(ClockQuality::Serial));
  write_mark(ClockQuality::Serial);
  return true;
}

bool apply_backend_hint(uint64_t unix_seconds) {
  init_once();
  const ClockQuality q = quality();
  if (q == ClockQuality::Ntp || q == ClockQuality::Serial) return false;  // never overrides a better source
  if (!in_range(unix_seconds)) return false;
  if (!set_system_time(unix_seconds)) return false;
  s_quality.store(static_cast<uint8_t>(ClockQuality::BackendHint));
  write_mark(ClockQuality::BackendHint);
  return true;
}

void start_sntp() {
  bool expected = false;
  if (!s_sntp_started.compare_exchange_strong(expected, true)) return;
  sntp_set_time_sync_notification_cb(on_sntp_sync);
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com", "time.google.com");
}

bool wait_ntp(uint32_t timeout_ms) {
  const uint32_t start = millis();
  while (!s_ntp_synced.load()) {
    if (millis() - start >= timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  return true;
}

bool ntp_synced_this_boot() { return s_ntp_synced.load(); }

uint64_t uptime_ms() { return static_cast<uint64_t>(esp_timer_get_time() / 1000); }

}  // namespace oyster::timesync
