// Wi-Fi STA uplink to the backend LAN (credentials from DeviceConfig). Non-blocking: an association
// attempt runs in the driver while callers keep working; failures back off up to 60 s. Starts SNTP
// once after the first association. The passphrase is never logged. Implementation: net_link.cpp.
#pragma once

#include <cstdint>

namespace oyster::net {

bool configured();   // an SSID is provisioned
bool up();           // associated and holding an IP address

// Returns true when the link is up. Otherwise starts (or keeps waiting on) an association attempt,
// waiting at most wait_ms here; respects the reconnect backoff.
bool ensure_up(uint32_t wait_ms);

int8_t rssi();       // 0 when not associated
uint32_t connect_attempts();

}  // namespace oyster::net
