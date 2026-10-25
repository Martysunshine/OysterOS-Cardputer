// Device clock bookkeeping (ADR-003): serial seed, bounded SNTP, backend GET /v1/time hint.
// clock_quality is recorded on every event; a backend hint never overrides an ntp or serial clock.
// UTC is reported only while the clock is plausible. Implementation: time_sync.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster::timesync {

constexpr uint64_t kMinPlausibleUnix = 1767225600ULL;  // 2026-01-01T00:00:00Z
constexpr uint64_t kMaxPlausibleUnix = 4102444800ULL;  // 2100-01-01T00:00:00Z

// Last known good UTC (provisioning seed, later NTP): a clock earlier than this is not plausible.
void set_floor(uint64_t last_known_good_unix);

bool plausible();
ClockQuality quality();               // Unknown whenever the clock is not plausible
bool now_unix(uint64_t& out);         // false when not plausible

// "2026-10-24T10:00:00.123Z" into out (>= 25 bytes). Returns the quality used; when it is Unknown
// out is "" and the caller records timestamp_utc as null.
ClockQuality format_utc(char* out, size_t n);

bool set_from_serial(uint64_t unix_seconds);     // provisioning SET-TIME: quality serial
bool apply_backend_hint(uint64_t unix_seconds);  // only while the clock is unknown or a hint
void start_sntp();                               // once per boot, after the STA link is up
bool wait_ntp(uint32_t timeout_ms);              // bounded wait for the first SNTP sync
bool ntp_synced_this_boot();

uint64_t uptime_ms();                            // 64-bit monotonic uptime (no 49-day wrap)

}  // namespace oyster::timesync
