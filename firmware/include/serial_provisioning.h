// SerialProvisioning: one-time local setup over USB serial (scripts/provision.ps1). Accepts a small
// line protocol to set device_id, backend host/port, Wi-Fi credentials, CA PEM, device token and a
// clock seed, then stores them via DeviceConfig. Values are acknowledged by field name only; secrets
// are never echoed back. Changes are staged in RAM and applied on DONE only (including the clock seed).
// Implementation: serial_provisioning.cpp.
#pragma once

#include <cstdint>

namespace oyster {

class SerialProvisioning {
 public:
  // Poll once per loop. Returns true while a provisioning session is active (UI shows a setup screen).
  // Commands (newline-terminated; full grammar in docs/PROVISIONING.md): HELLO, SET-ID, SET-BACKEND,
  // SET-WIFI, SET-TOKEN, CA-BEGIN/CA-LINE/CA-END, SET-TIME, TARGET-ADD <json>, TARGETS-CLEAR, STATUS,
  // DONE, FACTORY-RESET. Replies: OK <field>, ERR <reason>, STATUS <json-without-secrets>.
  bool poll();
  bool active() const;
  // Increments whenever the stored setup changed (FACTORY-RESET, DONE) so the UI can reload it.
  uint32_t config_generation() const;
};

extern SerialProvisioning g_provisioning;

}  // namespace oyster
