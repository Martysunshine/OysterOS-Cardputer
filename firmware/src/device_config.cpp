// DeviceConfigStore: NVS namespace "oyster" (Preferences) for settings and secrets, LittleFS for the
// backend CA PEM, enrolled targets as one versioned NVS blob. Secret values are never logged; log
// lines name keys only. Thread-safe (one mutex).
#include "device_config.h"

#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstring>

#include "oyster_core/ids.h"
#include "time_sync.h"

namespace oyster {

DeviceConfigStore g_config;

namespace {

constexpr const char* kNamespace = "oyster";
constexpr const char* kKeyHost = "host";
constexpr const char* kKeyPort = "port";
constexpr const char* kKeyDeviceId = "dev_id";
constexpr const char* kKeySsid = "ssid";
constexpr const char* kKeySeed = "time_seed";
constexpr const char* kKeyToken = "token";
constexpr const char* kKeyWifiPw = "wifi_pw";
constexpr const char* kKeyTargets = "targets";

constexpr const char* kCaDir = "/ca";
constexpr const char* kCaPath = "/ca/backend_ca.pem";
constexpr const char* kCaTmpPath = "/ca/backend_ca.tmp";
constexpr size_t kMaxCaBytes = 4096;

constexpr uint32_t kTargetsMagic = 0x3147544Fu;  // "OTG1"
constexpr uint16_t kTargetsVersion = 1;

struct TargetsBlobHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t count;
  uint16_t record_size;
  uint16_t reserved;
};

struct TargetsBlob {
  TargetsBlobHeader h;
  EnrolledTarget t[cfg::kMaxEnrolledTargets];
};

Preferences s_prefs;
SemaphoreHandle_t s_mu = nullptr;
bool s_nvs_ok = false;
bool s_fs_ok = false;

class Lock {
 public:
  Lock() {
    if (s_mu != nullptr) xSemaphoreTakeRecursive(s_mu, portMAX_DELAY);
  }
  ~Lock() {
    if (s_mu != nullptr) xSemaphoreGiveRecursive(s_mu);
  }
};

void ensure_mutex() {
  if (s_mu == nullptr) s_mu = xSemaphoreCreateRecursiveMutex();
}

bool get_str(const char* key, char* out, size_t n) {
  if (out == nullptr || n == 0) return false;
  out[0] = '\0';
  if (!s_nvs_ok || !s_prefs.isKey(key)) return false;
  return s_prefs.getString(key, out, n) > 0;
}

bool put_str(const char* key, const char* v) {
  if (!s_nvs_ok) return false;
  if (v == nullptr || v[0] == '\0') {
    if (s_prefs.isKey(key)) return s_prefs.remove(key);
    return true;
  }
  return s_prefs.putString(key, v) == std::strlen(v);
}

bool put_secret(const char* key, const char* v, bool allow_empty) {
  if (!s_nvs_ok || v == nullptr) return false;
  if (v[0] == '\0' && !allow_empty) {
    if (s_prefs.isKey(key)) return s_prefs.remove(key);
    return true;
  }
  // Preferences::putString returns the byte count; an empty string is stored as "" (length 0).
  const size_t want = std::strlen(v);
  const size_t wrote = s_prefs.putString(key, v);
  if (want == 0) return s_prefs.isKey(key);
  return wrote == want;
}

size_t ca_len_locked() {
  if (!s_fs_ok || !LittleFS.exists(kCaPath)) return 0;
  File f = LittleFS.open(kCaPath, FILE_READ);
  if (!f) return 0;
  const size_t n = f.size();
  f.close();
  return n;
}

bool token_present_locked() { return s_nvs_ok && s_prefs.isKey(kKeyToken); }

}  // namespace

bool DeviceConfigStore::begin() {
  ensure_mutex();
  Lock l;
  if (!s_nvs_ok) {
    s_nvs_ok = s_prefs.begin(kNamespace, false);
    if (!s_nvs_ok) log_e("config: NVS namespace open failed");
  }
  if (!s_fs_ok) {
    // Partition label "spiffs" (partitions_8mb_noota.csv) holds a LittleFS image; format on first use.
    s_fs_ok = LittleFS.begin(true);
    if (!s_fs_ok) log_e("config: LittleFS mount failed (CA unavailable)");
  }
  if (s_nvs_ok) {
    const uint64_t seed = s_prefs.isKey(kKeySeed) ? s_prefs.getULong64(kKeySeed, 0) : 0;
    timesync::set_floor(seed);
  }
  return s_nvs_ok;
}

bool DeviceConfigStore::load(DeviceConfig& out) const {
  ensure_mutex();
  Lock l;
  std::memset(&out, 0, sizeof(out));
  if (!s_nvs_ok) return false;
  get_str(kKeyHost, out.backend_host, sizeof(out.backend_host));
  out.backend_port = s_prefs.isKey(kKeyPort) ? s_prefs.getUShort(kKeyPort, 0) : 0;
  get_str(kKeyDeviceId, out.device_id, sizeof(out.device_id));
  get_str(kKeySsid, out.wifi_ssid, sizeof(out.wifi_ssid));
  out.time_seed_unix = s_prefs.isKey(kKeySeed) ? s_prefs.getULong64(kKeySeed, 0) : 0;
  out.provisioned = token_present_locked() && out.backend_host[0] != '\0' && out.backend_port != 0 &&
                    ca_len_locked() > 0;
  return true;
}

bool DeviceConfigStore::save(const DeviceConfig& c) {
  ensure_mutex();
  Lock l;
  if (!s_nvs_ok) return false;
  bool ok = put_str(kKeyHost, c.backend_host);
  if (c.backend_port != 0) {
    ok = (s_prefs.putUShort(kKeyPort, c.backend_port) == sizeof(uint16_t)) && ok;
  } else if (s_prefs.isKey(kKeyPort)) {
    ok = s_prefs.remove(kKeyPort) && ok;
  }
  ok = put_str(kKeyDeviceId, c.device_id) && ok;
  ok = put_str(kKeySsid, c.wifi_ssid) && ok;
  if (c.time_seed_unix != 0) {
    ok = (s_prefs.putULong64(kKeySeed, c.time_seed_unix) == sizeof(uint64_t)) && ok;
    timesync::set_floor(c.time_seed_unix);
  }
  if (!ok) log_e("config: NVS write failed");
  return ok;
}

bool DeviceConfigStore::set_device_token(const char* token) {
  ensure_mutex();
  Lock l;
  return put_secret(kKeyToken, token, false);
}

bool DeviceConfigStore::get_device_token(char* out, size_t n) const {
  ensure_mutex();
  Lock l;
  return get_str(kKeyToken, out, n);
}

bool DeviceConfigStore::set_wifi_password(const char* pw) {
  ensure_mutex();
  Lock l;
  return put_secret(kKeyWifiPw, pw, true);  // "" = open network
}

bool DeviceConfigStore::get_wifi_password(char* out, size_t n) const {
  ensure_mutex();
  Lock l;
  if (out == nullptr || n == 0) return false;
  out[0] = '\0';
  if (!s_nvs_ok || !s_prefs.isKey(kKeyWifiPw)) return false;
  return s_prefs.getString(kKeyWifiPw, out, n) > 0;  // length incl. NUL: 1 for "" (open network)
}

bool DeviceConfigStore::set_ca_pem(const char* pem, size_t len) {
  ensure_mutex();
  Lock l;
  if (!s_fs_ok) return false;
  if (pem == nullptr || len == 0) {
    if (LittleFS.exists(kCaPath)) return LittleFS.remove(kCaPath);
    return true;
  }
  if (len > kMaxCaBytes) return false;
  if (!LittleFS.exists(kCaDir)) LittleFS.mkdir(kCaDir);
  File f = LittleFS.open(kCaTmpPath, FILE_WRITE);
  if (!f) return false;
  const size_t w = f.write(reinterpret_cast<const uint8_t*>(pem), len);
  f.close();
  if (w != len) {
    LittleFS.remove(kCaTmpPath);
    return false;
  }
  if (LittleFS.exists(kCaPath)) LittleFS.remove(kCaPath);
  if (!LittleFS.rename(kCaTmpPath, kCaPath)) {
    log_e("config: CA rename failed");
    return false;
  }
  return true;
}

size_t DeviceConfigStore::ca_pem_len() const {
  ensure_mutex();
  Lock l;
  return ca_len_locked();
}

bool DeviceConfigStore::get_ca_pem(char* out, size_t n) const {
  ensure_mutex();
  Lock l;
  if (out == nullptr || n == 0) return false;
  out[0] = '\0';
  if (!s_fs_ok || !LittleFS.exists(kCaPath)) return false;
  File f = LittleFS.open(kCaPath, FILE_READ);
  if (!f) return false;
  const size_t len = f.size();
  if (len == 0 || len + 1 > n) {
    f.close();
    return false;
  }
  const size_t r = f.read(reinterpret_cast<uint8_t*>(out), len);
  f.close();
  if (r != len) {
    out[0] = '\0';
    return false;
  }
  out[len] = '\0';
  return true;
}

size_t DeviceConfigStore::load_targets(EnrolledTarget* out, size_t max) const {
  ensure_mutex();
  Lock l;
  if (out == nullptr || max == 0 || !s_nvs_ok || !s_prefs.isKey(kKeyTargets)) return 0;
  const size_t len = s_prefs.getBytesLength(kKeyTargets);
  if (len < sizeof(TargetsBlobHeader) || len > sizeof(TargetsBlob)) return 0;
  static TargetsBlob blob;  // guarded by s_mu; too large for small task stacks
  std::memset(&blob, 0, sizeof(blob));
  if (s_prefs.getBytes(kKeyTargets, &blob, len) != len) return 0;
  if (blob.h.magic != kTargetsMagic || blob.h.version != kTargetsVersion ||
      blob.h.record_size != sizeof(EnrolledTarget) || blob.h.count > cfg::kMaxEnrolledTargets ||
      len != sizeof(TargetsBlobHeader) + blob.h.count * sizeof(EnrolledTarget)) {
    log_w("config: enrolled target blob has another layout; ignored (re-provision targets)");
    return 0;
  }
  size_t n = 0;
  for (size_t i = 0; i < blob.h.count && n < max; ++i) {
    EnrolledTarget& t = blob.t[i];
    t.target_id[sizeof(t.target_id) - 1] = '\0';
    t.profile_id[sizeof(t.profile_id) - 1] = '\0';
    t.advertised_name[sizeof(t.advertised_name) - 1] = '\0';
    if (!is_valid_target_id(t.target_id)) continue;
    out[n++] = t;
  }
  return n;
}

bool DeviceConfigStore::save_targets(const EnrolledTarget* t, size_t n) {
  ensure_mutex();
  Lock l;
  if (!s_nvs_ok || n > cfg::kMaxEnrolledTargets || (n > 0 && t == nullptr)) return false;
  if (n == 0) {
    if (s_prefs.isKey(kKeyTargets)) return s_prefs.remove(kKeyTargets);
    return true;
  }
  static TargetsBlob blob;
  std::memset(&blob, 0, sizeof(blob));
  blob.h.magic = kTargetsMagic;
  blob.h.version = kTargetsVersion;
  blob.h.count = static_cast<uint16_t>(n);
  blob.h.record_size = sizeof(EnrolledTarget);
  for (size_t i = 0; i < n; ++i) blob.t[i] = t[i];
  const size_t len = sizeof(TargetsBlobHeader) + n * sizeof(EnrolledTarget);
  return s_prefs.putBytes(kKeyTargets, &blob, len) == len;
}

bool DeviceConfigStore::is_provisioned() const {
  DeviceConfig c{};
  return load(c) && c.provisioned;
}

bool DeviceConfigStore::factory_reset() {
  ensure_mutex();
  Lock l;
  bool ok = true;
  if (s_nvs_ok) {
    for (const char* key : {kKeyToken, kKeyWifiPw, kKeyTargets}) {
      if (s_prefs.isKey(key)) ok = s_prefs.remove(key) && ok;
    }
  } else {
    ok = false;
  }
  if (s_fs_ok) {
    if (LittleFS.exists(kCaPath)) ok = LittleFS.remove(kCaPath) && ok;
    if (LittleFS.exists(kCaTmpPath)) LittleFS.remove(kCaTmpPath);
  }
  return ok;
}

}  // namespace oyster
