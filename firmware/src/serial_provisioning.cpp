// SerialProvisioning: docs/PROVISIONING.md protocol v1 over the USB CDC serial port. Non-blocking
// line reader (polled from loop()); changes are staged in RAM and persisted only on DONE; 60 s
// without a command aborts the session. Replies name fields only: the token, Wi-Fi password and CA
// are never echoed or logged, and staged secrets are wiped after DONE/ABORT/timeout.
// Maximum line length 512 bytes (the lab TARGET-ADD line is ~414). CA-BEGIN and CA-LINE reply only
// on error; CA-END replies OK ca <bytes>. SET-TIME is staged like the rest: the live clock is set on DONE
// only. FACTORY-RESET and DONE drop the backend client's cached credentials and reload the runner's targets.
#include "serial_provisioning.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <mbedtls/x509_crt.h>

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "assessment_runner.h"
#include "backend_client.h"
#include "board_support.h"
#include "device_config.h"
#include "evidence_store.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "oyster_core/local_policy.h"
#include "time_sync.h"

#ifndef OYSTER_FIRMWARE_VERSION
#define OYSTER_FIRMWARE_VERSION "0.0.0-dev"
#endif

namespace oyster {

SerialProvisioning g_provisioning;

namespace {

constexpr size_t kLineBuf = 512;  // docs/PROVISIONING.md maximum line length (without the newline)
constexpr uint32_t kIdleAbortMs = 60000;
constexpr size_t kMaxCaBytes = 4096;
constexpr size_t kMaxReadPerPoll = 512;
constexpr size_t kMinTokenLen = 16;
constexpr size_t kMaxTokenLen = 128;
constexpr size_t kMaxAiKeyLen = 200;    // provider API key (direct AI); a secret, never echoed
constexpr size_t kMaxAiModelLen = 64;   // provider model id (direct AI)

struct Staged {
  bool device_id_set;
  char device_id[cfg::kIdBufLen];
  bool backend_set;
  char host[64];
  uint16_t port;
  bool wifi_set;
  char ssid[33];
  char password[65];
  bool token_set;
  char token[kMaxTokenLen + 1];
  bool ai_key_set;
  char ai_key[kMaxAiKeyLen + 1];
  bool ai_model_set;
  char ai_model[kMaxAiModelLen + 1];
  bool ca_set;
  bool time_set;
  uint64_t time_seed;
  bool targets_touched;
  EnrolledTarget targets[cfg::kMaxEnrolledTargets];
  size_t target_count;
};

Staged s_st;
char s_ca[kMaxCaBytes + 1];
size_t s_ca_len = 0;
bool s_ca_receiving = false;
bool s_ca_overflow = false;
char s_line[kLineBuf + 1];
size_t s_line_len = 0;
bool s_line_overflow = false;
bool s_active = false;
uint32_t s_last_cmd_ms = 0;
uint32_t s_config_gen = 0;

void secure_zero(void* p, size_t n) {
  volatile uint8_t* v = static_cast<volatile uint8_t*>(p);
  while (n--) *v++ = 0;
}

void reply(const char* fmt, ...) {
  char buf[200];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.print(buf);
  Serial.print('\n');
}

void clear_staged() {
  secure_zero(&s_st, sizeof(s_st));
  secure_zero(s_ca, sizeof(s_ca));
  s_ca_len = 0;
  s_ca_receiving = false;
  s_ca_overflow = false;
}

void start_session() {
  clear_staged();
  s_st.target_count = g_config.load_targets(s_st.targets, cfg::kMaxEnrolledTargets);
  s_active = true;
}

void end_session() {
  clear_staged();
  s_active = false;
}

int hex_val(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Decodes lowercase/uppercase hex into out; false on odd length, bad digit or overflow.
bool hex_decode(const char* in, uint8_t* out, size_t cap, size_t& out_len) {
  out_len = 0;
  const size_t n = std::strlen(in);
  if (n % 2 != 0 || n / 2 > cap) return false;
  for (size_t i = 0; i < n; i += 2) {
    const int hi = hex_val(in[i]);
    const int lo = hex_val(in[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[out_len++] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}

bool valid_host(const char* h) {
  const size_t n = std::strlen(h);
  if (n == 0 || n > 63) return false;
  for (size_t i = 0; i < n; ++i) {
    const char c = h[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '-';
    if (!ok) return false;
  }
  return h[0] != '-' && h[0] != '.';
}

bool parse_u64(const char* s, uint64_t& out) {
  if (s == nullptr || *s == '\0') return false;
  uint64_t v = 0;
  for (const char* p = s; *p != '\0'; ++p) {
    if (*p < '0' || *p > '9') return false;
    const uint64_t d = static_cast<uint64_t>(*p - '0');
    if (v > (UINT64_MAX - d) / 10) return false;
    v = v * 10 + d;
  }
  out = v;
  return true;
}

bool parse_mac(const char* s, uint8_t out[6]) {
  if (s == nullptr || std::strlen(s) != 17) return false;
  for (int i = 0; i < 6; ++i) {
    const int hi = hex_val(s[i * 3]);
    const int lo = hex_val(s[i * 3 + 1]);
    if (hi < 0 || lo < 0) return false;
    if (i < 5 && s[i * 3 + 2] != ':') return false;
    out[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}

// Splits off the next space-separated token. Returns nullptr when none is left.
char* next_token(char*& p) {
  while (*p == ' ') ++p;
  if (*p == '\0') return nullptr;
  char* start = p;
  while (*p != '\0' && *p != ' ') ++p;
  if (*p == ' ') *p++ = '\0';
  return start;
}

// The stored setup changed (or may have, after a failed write): drop what the backend client and the runner
// cached from it so no job is polled or executed with the old token, CA or target list, and tell the UI.
void config_changed() {
  ++s_config_gen;
  if (g_board.info().is_cardputer_adv) {  // backend and runner only run on the ADV
    g_backend.invalidate_credentials();
    g_runner.reload_targets();
  }
}

bool stored_token_present() {
  char buf[kMaxTokenLen + 1];
  const bool present = g_config.get_device_token(buf, sizeof(buf)) && buf[0] != '\0';
  secure_zero(buf, sizeof(buf));
  return present;
}

// ---- command handlers -------------------------------------------------------------------------------

void cmd_hello(char* args) {
  char* ver = next_token(args);
  if (ver == nullptr || std::strcmp(ver, "oyster-prov/1") != 0) {
    reply("ERR unsupported_protocol expected oyster-prov/1");
    return;
  }
  start_session();
  const char* board = g_board.info().is_cardputer_adv ? "cardputer-adv" : "unknown-board";
  reply("OK hello %s %s", OYSTER_FIRMWARE_VERSION, board);
}

void cmd_set_id(char* args) {
  char* id = next_token(args);
  if (id == nullptr || !is_valid_device_id(id) || next_token(args) != nullptr) {
    reply("ERR invalid_device_id expected [0-9a-zA-Z_-]{1,40}");
    return;
  }
  copy_bounded(s_st.device_id, sizeof(s_st.device_id), id);
  s_st.device_id_set = true;
  reply("OK device_id");
}

void cmd_set_backend(char* args) {
  char* host = next_token(args);
  char* port_s = next_token(args);
  uint64_t port = 0;
  if (host == nullptr || port_s == nullptr || next_token(args) != nullptr || !valid_host(host) ||
      !parse_u64(port_s, port) || port == 0 || port > 65535) {
    reply("ERR invalid_backend expected <host> <port>");
    return;
  }
  copy_bounded(s_st.host, sizeof(s_st.host), host);
  s_st.port = static_cast<uint16_t>(port);
  s_st.backend_set = true;
  reply("OK backend");
}

void cmd_set_wifi(char* args) {
  char* ssid_hex = next_token(args);
  char* pw_hex = next_token(args);  // absent or "-" = open network
  if (ssid_hex == nullptr || next_token(args) != nullptr) {
    reply("ERR invalid_wifi expected <ssid_hex> <password_hex>");
    return;
  }
  uint8_t ssid[32];
  size_t ssid_len = 0;
  if (!hex_decode(ssid_hex, ssid, sizeof(ssid), ssid_len) || ssid_len == 0 ||
      std::memchr(ssid, 0, ssid_len) != nullptr) {
    reply("ERR invalid_wifi ssid must be 1-32 bytes, hex encoded, without NUL");
    return;
  }
  uint8_t pw[64];
  size_t pw_len = 0;
  bool pw_ok = true;
  if (pw_hex != nullptr && std::strcmp(pw_hex, "-") != 0) {
    pw_ok = hex_decode(pw_hex, pw, sizeof(pw), pw_len);
    if (pw_ok) {
      bool all_hex = pw_len == 64;
      for (size_t i = 0; i < pw_len; ++i) {
        if (pw[i] < 0x20 || pw[i] > 0x7E) pw_ok = false;
        if (hex_val(static_cast<char>(pw[i])) < 0) all_hex = false;
      }
      // WPA passphrase 8..63 printable ASCII, or a 64-digit hex PSK.
      if (!((pw_len >= 8 && pw_len <= 63) || all_hex)) pw_ok = false;
    }
  }
  if (!pw_ok) {
    secure_zero(pw, sizeof(pw));
    reply("ERR invalid_wifi password must be 8-63 printable ASCII or 64 hex digits (hex encoded)");
    return;
  }
  std::memcpy(s_st.ssid, ssid, ssid_len);
  s_st.ssid[ssid_len] = '\0';
  secure_zero(s_st.password, sizeof(s_st.password));
  std::memcpy(s_st.password, pw, pw_len);
  s_st.password[pw_len] = '\0';
  secure_zero(pw, sizeof(pw));
  s_st.wifi_set = true;
  reply("OK wifi");
}

void cmd_set_token(char* args) {
  char* tok = next_token(args);
  bool ok = tok != nullptr && next_token(args) == nullptr;
  size_t n = ok ? std::strlen(tok) : 0;
  if (ok && (n < kMinTokenLen || n > kMaxTokenLen)) ok = false;
  for (size_t i = 0; ok && i < n; ++i) {
    if (tok[i] <= 0x20 || tok[i] > 0x7E) ok = false;
  }
  if (!ok) {
    reply("ERR invalid_token expected 16-128 printable characters");
    return;
  }
  secure_zero(s_st.token, sizeof(s_st.token));
  copy_bounded(s_st.token, sizeof(s_st.token), tok);
  s_st.token_set = true;
  reply("OK token");
}

// Direct-AI provider key (a secret: never echoed). Printable, bounded.
void cmd_set_ai_key(char* args) {
  char* key = next_token(args);
  bool ok = key != nullptr && next_token(args) == nullptr;
  size_t n = ok ? std::strlen(key) : 0;
  if (ok && (n < 8 || n > kMaxAiKeyLen)) ok = false;
  for (size_t i = 0; ok && i < n; ++i) {
    if (key[i] <= 0x20 || key[i] > 0x7E) ok = false;
  }
  if (!ok) {
    reply("ERR invalid_ai_key expected 8-%u printable characters", static_cast<unsigned>(kMaxAiKeyLen));
    return;
  }
  secure_zero(s_st.ai_key, sizeof(s_st.ai_key));
  copy_bounded(s_st.ai_key, sizeof(s_st.ai_key), key);
  s_st.ai_key_set = true;
  reply("OK ai_key");
}

void cmd_set_ai_model(char* args) {
  char* model = next_token(args);
  bool ok = model != nullptr && next_token(args) == nullptr;
  size_t n = ok ? std::strlen(model) : 0;
  if (ok && (n == 0 || n > kMaxAiModelLen)) ok = false;
  for (size_t i = 0; ok && i < n; ++i) {
    const char ch = model[i];
    if (!(std::isalnum(static_cast<unsigned char>(ch)) || ch == '/' || ch == '-' || ch == '.' || ch == '_')) {
      ok = false;
    }
  }
  if (!ok) {
    reply("ERR invalid_ai_model expected a model id");
    return;
  }
  copy_bounded(s_st.ai_model, sizeof(s_st.ai_model), model);
  s_st.ai_model_set = true;
  reply("OK ai_model %s", s_st.ai_model);
}

void cmd_ca_begin() {
  secure_zero(s_ca, sizeof(s_ca));
  s_ca_len = 0;
  s_ca_receiving = true;
  s_ca_overflow = false;
}

void cmd_ca_line(const char* rest) {
  if (!s_ca_receiving) {
    reply("ERR ca_not_started send CA-BEGIN first");
    return;
  }
  const size_t n = std::strlen(rest);
  if (s_ca_len + n + 1 > kMaxCaBytes) {
    if (!s_ca_overflow) reply("ERR ca_too_large maximum 4096 bytes");
    s_ca_overflow = true;
    return;
  }
  std::memcpy(s_ca + s_ca_len, rest, n);
  s_ca_len += n;
  s_ca[s_ca_len++] = '\n';
  s_ca[s_ca_len] = '\0';
}

void cmd_ca_end() {
  if (!s_ca_receiving) {
    reply("ERR ca_not_started send CA-BEGIN first");
    return;
  }
  s_ca_receiving = false;
  if (s_ca_overflow || s_ca_len == 0) {
    reply(s_ca_overflow ? "ERR ca_too_large maximum 4096 bytes" : "ERR ca_empty");
    secure_zero(s_ca, sizeof(s_ca));
    s_ca_len = 0;
    s_st.ca_set = false;
    return;
  }
  // Must parse as at least one X.509 certificate (mbedTLS wants the terminating NUL in the length).
  mbedtls_x509_crt crt;
  mbedtls_x509_crt_init(&crt);
  const int rc = mbedtls_x509_crt_parse(&crt, reinterpret_cast<const unsigned char*>(s_ca), s_ca_len + 1);
  mbedtls_x509_crt_free(&crt);
  if (rc != 0 || std::strstr(s_ca, "-----BEGIN CERTIFICATE-----") == nullptr) {
    reply("ERR ca_invalid not a PEM X.509 certificate (mbedtls %d)", rc);
    secure_zero(s_ca, sizeof(s_ca));
    s_ca_len = 0;
    s_st.ca_set = false;
    return;
  }
  s_st.ca_set = true;
  reply("OK ca %u", static_cast<unsigned>(s_ca_len));
}

void cmd_set_time(char* args) {
  char* t = next_token(args);
  uint64_t v = 0;
  // Range check only: the live clock is set on DONE (docs/PROVISIONING.md: staged until DONE).
  if (t == nullptr || next_token(args) != nullptr || !parse_u64(t, v) || v < timesync::kMinPlausibleUnix ||
      v >= timesync::kMaxPlausibleUnix) {
    reply("ERR invalid_time expected unix seconds between 2026-01-01 and 2100-01-01");
    return;
  }
  s_st.time_set = true;
  s_st.time_seed = v;
  reply("OK time");
}

const char* parse_target(const char* json, size_t len, EnrolledTarget& t) {
  std::memset(&t, 0, sizeof(t));
  t.address_type = BleAddrType::Unknown;
  JsonDocument doc;
  if (deserializeJson(doc, json, len) != DeserializationError::Ok || !doc.is<JsonObjectConst>()) return "invalid_json";
  JsonObjectConst o = doc.as<JsonObjectConst>();
  static const char* const kTopKeys[] = {"schema_version", "target_id", "display_name", "profile_id",
                                         "scope_version", "allowed_operations", "identity", "notes"};
  for (JsonPairConst kv : o) {
    bool known = false;
    for (const char* k : kTopKeys) known = known || std::strcmp(kv.key().c_str(), k) == 0;
    if (!known) return "unknown_field";
  }
  if (!o["schema_version"].isNull()) {
    const char* sv = o["schema_version"].as<const char*>();
    if (sv == nullptr || std::strcmp(sv, "1.0") != 0) return "schema_version";
  }
  const char* tid = o["target_id"].as<const char*>();
  if (tid == nullptr || !is_valid_target_id(tid)) return "target_id";
  copy_bounded(t.target_id, sizeof(t.target_id), tid);
  if (!o["display_name"].isNull()) {
    const char* dn = o["display_name"].as<const char*>();
    if (dn == nullptr || std::strlen(dn) > 64) return "display_name";
  }
  if (!o["notes"].isNull()) {
    const char* nt = o["notes"].as<const char*>();
    if (nt == nullptr || std::strlen(nt) > 400) return "notes";
  }
  const char* pid = o["profile_id"].as<const char*>();
  if (pid == nullptr || pid[0] == '\0' || std::strlen(pid) >= sizeof(t.profile_id)) return "profile_id";
  copy_bounded(t.profile_id, sizeof(t.profile_id), pid);
  JsonVariantConst sv = o["scope_version"];
  if (!sv.is<uint32_t>() || sv.as<uint32_t>() < 1) return "scope_version";
  t.scope_version = sv.as<uint32_t>();
  JsonVariantConst ops = o["allowed_operations"];
  if (!ops.is<JsonArrayConst>()) return "allowed_operations";
  for (JsonVariantConst v : ops.as<JsonArrayConst>()) {
    const char* name = v.as<const char*>();
    Operation op = Operation::Unknown;
    if (name == nullptr || !parse_operation(name, op) || !operation_requires_target(op)) return "allowed_operations";
    if ((t.allowed_ops_mask & op_bit(op)) != 0) return "allowed_operations";  // uniqueItems
    t.allowed_ops_mask = static_cast<uint16_t>(t.allowed_ops_mask | op_bit(op));
  }
  JsonVariantConst idv = o["identity"];
  if (!idv.is<JsonObjectConst>()) return "identity";
  JsonObjectConst id = idv.as<JsonObjectConst>();
  for (JsonPairConst kv : id) {
    const char* k = kv.key().c_str();
    if (std::strcmp(k, "advertised_name") != 0 && std::strcmp(k, "service_uuid") != 0 &&
        std::strcmp(k, "address") != 0 && std::strcmp(k, "address_type") != 0) {
      return "identity";
    }
  }
  bool any = false;
  if (!id["advertised_name"].isNull()) {
    const char* nm = id["advertised_name"].as<const char*>();
    if (nm == nullptr || nm[0] == '\0' || std::strlen(nm) > cfg::kMaxBleNameBytes) return "advertised_name";
    copy_bounded(t.advertised_name, sizeof(t.advertised_name), nm);
    any = true;
  }
  if (!id["service_uuid"].isNull()) {
    const char* u = id["service_uuid"].as<const char*>();
    if (u == nullptr || !parse_uuid128(u, t.service_uuid128)) return "service_uuid";
    t.has_service_uuid = true;
    any = true;
  }
  if (!id["address"].isNull()) {
    const char* a = id["address"].as<const char*>();
    if (a == nullptr || !parse_mac(a, t.address)) return "address";
    t.has_address = true;
    any = true;
  }
  if (!id["address_type"].isNull()) {
    const char* at = id["address_type"].as<const char*>();
    if (at == nullptr) return "address_type";
    if (std::strcmp(at, "public") == 0) {
      t.address_type = BleAddrType::Public;
    } else if (std::strcmp(at, "random") == 0) {
      t.address_type = BleAddrType::Random;
    } else if (std::strcmp(at, "unknown") == 0) {
      t.address_type = BleAddrType::Unknown;
    } else {
      return "address_type";
    }
  }
  if (!any) return "identity_missing";
  return nullptr;
}

void cmd_target_add(const char* rest) {
  EnrolledTarget t;
  const char* err = parse_target(rest, std::strlen(rest), t);
  if (err != nullptr) {
    reply("ERR invalid_target %s", err);
    return;
  }
  size_t slot = s_st.target_count;
  for (size_t i = 0; i < s_st.target_count; ++i) {
    if (std::strcmp(s_st.targets[i].target_id, t.target_id) == 0) slot = i;  // replace
  }
  if (slot >= cfg::kMaxEnrolledTargets) {
    reply("ERR too_many_targets maximum 8");
    return;
  }
  s_st.targets[slot] = t;
  if (slot == s_st.target_count) ++s_st.target_count;
  s_st.targets_touched = true;
  reply("OK target %s", t.target_id);
}

void cmd_targets_clear() {
  std::memset(s_st.targets, 0, sizeof(s_st.targets));
  s_st.target_count = 0;
  s_st.targets_touched = true;
  reply("OK targets");
}

void cmd_status() {
  DeviceConfig c{};
  g_config.load(c);
  char buf[400];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  const char* dev = s_active && s_st.device_id_set ? s_st.device_id : c.device_id;
  if (dev[0] != '\0') {
    w.str("device_id", dev);
  } else {
    w.null("device_id");
  }
  const char* host = s_active && s_st.backend_set ? s_st.host : c.backend_host;
  const uint16_t port = s_active && s_st.backend_set ? s_st.port : c.backend_port;
  if (host[0] != '\0' && port != 0) {
    char hp[80];
    snprintf(hp, sizeof(hp), "%s:%u", host, static_cast<unsigned>(port));
    w.str("backend", hp);
  } else {
    w.null("backend");
  }
  w.boolean("wifi_ssid_set", (s_active && s_st.wifi_set) || c.wifi_ssid[0] != '\0');
  w.boolean("token_set", (s_active && s_st.token_set) || stored_token_present());
  w.boolean("ai_key_set", (s_active && s_st.ai_key_set) || g_config.ai_key_set());
  w.unum("ca_bytes", s_active && s_st.ca_set ? s_ca_len : g_config.ca_pem_len());
  const size_t targets = s_active ? s_st.target_count : g_config.load_targets(s_st.targets, cfg::kMaxEnrolledTargets);
  if (!s_active) std::memset(s_st.targets, 0, sizeof(s_st.targets));
  w.unum("targets", targets);
  w.str("clock_quality", to_string(timesync::quality()));
  w.boolean("provisioned", c.provisioned);
  w.end_object();
  if (!w.ok()) {
    reply("ERR internal status too large");
    return;
  }
  reply("STATUS %s", buf);
}

void cmd_done() {
  DeviceConfig c{};
  g_config.load(c);
  const bool have_id = s_st.device_id_set || c.device_id[0] != '\0';
  const bool have_backend = s_st.backend_set || (c.backend_host[0] != '\0' && c.backend_port != 0);
  const bool have_wifi = s_st.wifi_set || c.wifi_ssid[0] != '\0';
  const bool have_token = s_st.token_set || stored_token_present();
  const bool have_ca = s_st.ca_set || g_config.ca_pem_len() > 0;
  if (s_ca_receiving) {
    reply("ERR incomplete ca (CA-END missing)");
    return;
  }
  if (!(have_id && have_backend && have_wifi && have_token && have_ca)) {
    char missing[96] = "";
    auto add = [&](bool have, const char* name) {
      if (have) return;
      if (missing[0] != '\0') strlcat(missing, ",", sizeof(missing));
      strlcat(missing, name, sizeof(missing));
    };
    add(have_id, "device_id");
    add(have_backend, "backend");
    add(have_wifi, "wifi");
    add(have_token, "token");
    add(have_ca, "ca");
    reply("ERR incomplete %s", missing);
    return;
  }
  if (s_st.device_id_set) copy_bounded(c.device_id, sizeof(c.device_id), s_st.device_id);
  if (s_st.backend_set) {
    copy_bounded(c.backend_host, sizeof(c.backend_host), s_st.host);
    c.backend_port = s_st.port;
  }
  if (s_st.wifi_set) copy_bounded(c.wifi_ssid, sizeof(c.wifi_ssid), s_st.ssid);
  if (s_st.time_set) c.time_seed_unix = s_st.time_seed;
  const char* failed = nullptr;
  if (!g_config.save(c)) failed = "settings";
  if (failed == nullptr && s_st.token_set && !g_config.set_device_token(s_st.token)) failed = "token";
  if (failed == nullptr && s_st.ai_key_set && !g_config.set_ai_key(s_st.ai_key)) failed = "ai_key";
  if (failed == nullptr && s_st.ai_model_set && !g_config.set_ai_model(s_st.ai_model)) failed = "ai_model";
  if (failed == nullptr && s_st.wifi_set && !g_config.set_wifi_password(s_st.password)) failed = "wifi";
  if (failed == nullptr && s_st.ca_set && !g_config.set_ca_pem(s_ca, s_ca_len)) failed = "ca";
  if (failed == nullptr && s_st.targets_touched && !g_config.save_targets(s_st.targets, s_st.target_count)) {
    failed = "targets";
  }
  if (failed != nullptr) {
    reply("ERR storage %s could not be written; staged changes kept", failed);
    config_changed();  // some fields may have been written
    return;
  }
  // The staged clock seed becomes the live clock only now that the setup is stored.
  if (s_st.time_set && !timesync::set_from_serial(s_st.time_seed)) {
    reply("ERR clock could not be set; staged changes kept");
    config_changed();
    return;
  }
  reply("OK done");
  config_changed();
  end_session();
  Serial.flush();
  g_evidence.close_session();
  delay(300);
  ESP.restart();
}

void handle_line(char* line) {
  char* p = line;
  while (*p == ' ') ++p;
  if (*p == '\0') return;
  char* rest = p;
  char* cmd = next_token(rest);
  if (cmd == nullptr) return;
  if (std::strcmp(cmd, "HELLO") == 0) {
    s_last_cmd_ms = millis();
    cmd_hello(rest);
    return;
  }
  if (std::strcmp(cmd, "STATUS") == 0) {
    if (s_active) s_last_cmd_ms = millis();
    cmd_status();
    return;
  }
  static const char* const kKnown[] = {"SET-ID", "SET-BACKEND", "SET-WIFI", "SET-TOKEN", "SET-AI-KEY",
                                       "SET-AI-MODEL", "CA-BEGIN", "CA-LINE", "CA-END", "SET-TIME", "TARGET-ADD",
                                       "TARGETS-CLEAR", "DONE", "ABORT", "FACTORY-RESET"};
  bool known = false;
  for (const char* k : kKnown) known = known || std::strcmp(cmd, k) == 0;
  if (!known) {
    reply("ERR unknown_command");
    return;
  }
  if (!s_active) {
    reply("ERR no_session send HELLO oyster-prov/1 first");
    return;
  }
  s_last_cmd_ms = millis();
  if (std::strcmp(cmd, "SET-ID") == 0) {
    cmd_set_id(rest);
  } else if (std::strcmp(cmd, "SET-BACKEND") == 0) {
    cmd_set_backend(rest);
  } else if (std::strcmp(cmd, "SET-WIFI") == 0) {
    cmd_set_wifi(rest);
  } else if (std::strcmp(cmd, "SET-TOKEN") == 0) {
    cmd_set_token(rest);
  } else if (std::strcmp(cmd, "SET-AI-KEY") == 0) {
    cmd_set_ai_key(rest);
  } else if (std::strcmp(cmd, "SET-AI-MODEL") == 0) {
    cmd_set_ai_model(rest);
  } else if (std::strcmp(cmd, "CA-BEGIN") == 0) {
    cmd_ca_begin();
  } else if (std::strcmp(cmd, "CA-LINE") == 0) {
    cmd_ca_line(rest);  // the PEM line verbatim (may contain spaces)
  } else if (std::strcmp(cmd, "CA-END") == 0) {
    cmd_ca_end();
  } else if (std::strcmp(cmd, "SET-TIME") == 0) {
    cmd_set_time(rest);
  } else if (std::strcmp(cmd, "TARGET-ADD") == 0) {
    cmd_target_add(rest);
  } else if (std::strcmp(cmd, "TARGETS-CLEAR") == 0) {
    cmd_targets_clear();
  } else if (std::strcmp(cmd, "DONE") == 0) {
    cmd_done();
  } else if (std::strcmp(cmd, "ABORT") == 0) {
    end_session();
    reply("OK abort");
  } else if (std::strcmp(cmd, "FACTORY-RESET") == 0) {
    const bool ok = g_config.factory_reset();
    s_st.token_set = false;
    secure_zero(s_st.token, sizeof(s_st.token));
    s_st.wifi_set = false;
    secure_zero(s_st.password, sizeof(s_st.password));
    s_st.ca_set = false;
    secure_zero(s_ca, sizeof(s_ca));
    s_ca_len = 0;
    std::memset(s_st.targets, 0, sizeof(s_st.targets));
    s_st.target_count = 0;
    s_st.targets_touched = false;
    if (ok) {
      reply("OK factory_reset");
    } else {
      reply("ERR storage factory reset incomplete");
    }
    config_changed();  // after the reply; the cached token, CA and targets must not outlive the reset
  }
}

}  // namespace

bool SerialProvisioning::poll() {
  size_t budget = kMaxReadPerPoll;
  while (budget-- > 0 && Serial.available() > 0) {
    const int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      if (s_line_overflow) {
        reply("ERR line_too_long maximum %u bytes", static_cast<unsigned>(kLineBuf));
      } else {
        s_line[s_line_len] = '\0';
        handle_line(s_line);
      }
      secure_zero(s_line, sizeof(s_line));  // lines may carry the token or the Wi-Fi password
      s_line_len = 0;
      s_line_overflow = false;
      continue;
    }
    if (s_line_len < kLineBuf) {
      s_line[s_line_len++] = static_cast<char>(c);
    } else {
      s_line_overflow = true;
    }
  }
  if (s_active && millis() - s_last_cmd_ms > kIdleAbortMs) {
    end_session();
    reply("ERR timeout no command for 60 s; staged changes discarded");
  }
  return s_active;
}

bool SerialProvisioning::active() const { return s_active; }

uint32_t SerialProvisioning::config_generation() const { return s_config_gen; }

}  // namespace oyster
