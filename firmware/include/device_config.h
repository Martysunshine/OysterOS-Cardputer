// DeviceConfig: persistent device settings in NVS. Holds backend URL, per-device token,
// Wi-Fi credentials, the backend CA reference and clock seed. Secrets are never written to serial or
// SD and never leave the device. Implementation: device_config.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/local_policy.h"

namespace oyster {

struct DeviceConfig {
  char backend_host[64];   // IP or hostname the cert must match
  uint16_t backend_port;
  char device_id[48];
  char wifi_ssid[33];
  bool provisioned;        // token + backend + CA all present
  uint64_t time_seed_unix; // last known good UTC seconds, set at provisioning
};

class DeviceConfigStore {
 public:
  bool begin();                               // open NVS namespace "oyster"
  bool load(DeviceConfig& out) const;
  bool save(const DeviceConfig& c);

  // Secrets: setters take, getters fill caller buffers. get_* return false when absent.
  bool set_device_token(const char* token);
  bool get_device_token(char* out, size_t n) const;
  bool set_wifi_password(const char* pw);
  bool get_wifi_password(char* out, size_t n) const;

  // Direct AI (the device calls the provider itself over Wi-Fi, no backend). The key is a secret; the model
  // id is a plain setting. ai_key_set() is true once a key is stored.
  bool set_ai_key(const char* key);
  bool get_ai_key(char* out, size_t n) const;
  bool ai_key_set() const;
  bool set_ai_model(const char* model);
  bool get_ai_model(char* out, size_t n) const;  // fills a default when unset; always true

  // Backend CA PEM lives in LittleFS (/ca/backend_ca.pem) so it rotates without reflashing.
  bool set_ca_pem(const char* pem, size_t len);
  size_t ca_pem_len() const;
  bool get_ca_pem(char* out, size_t n) const;

  // Enrolled targets (provisioned locally; the device never trusts a backend-supplied target list).
  size_t load_targets(EnrolledTarget* out, size_t max) const;
  bool save_targets(const EnrolledTarget* t, size_t n);

  bool is_provisioned() const;
  bool factory_reset();                       // clears token, Wi-Fi password, CA, targets; keeps device_id
};

extern DeviceConfigStore g_config;

}  // namespace oyster
