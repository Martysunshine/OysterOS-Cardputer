// Clock persistence rule (ADR-003). Unauthenticated NTP must not be able to raise the persisted time floor
// by an arbitrary amount: a spoofed far-future reply would otherwise make the true time "implausible" until
// the device is re-provisioned. Header-only so the host tests cover the exact device rule.
#pragma once

#include <cstdint>

namespace oyster {

// An NTP time may move the persisted floor forward by at most this much (400 days).
constexpr uint64_t kMaxNtpFloorAdvanceSeconds = 400ULL * 24ULL * 3600ULL;

// current_floor_unix = the effective floor (persisted seed, or the firmware minimum when none).
// True only when ntp_unix is later than the floor and not further than kMaxNtpFloorAdvanceSeconds after it;
// otherwise the NTP time is used for the RAM clock only and is not persisted.
inline bool ntp_floor_may_advance(uint64_t ntp_unix, uint64_t current_floor_unix) {
  return ntp_unix > current_floor_unix && ntp_unix - current_floor_unix <= kMaxNtpFloorAdvanceSeconds;
}

}  // namespace oyster
