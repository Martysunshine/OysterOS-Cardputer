// BackendClient: HTTPS to the backend over one persistent, validated TLS connection
// (WiFiClientSecure + provisioned CA PEM, never setInsecure). Minimal bounded HTTP/1.1
// (Content-Length or chunked), bodies <= cfg::kMaxApiPageBytes, ArduinoJson 7 for responses.
// Called from the worker task only (one network owner); the UI reads state()/latest_run().
// Every request pauses while the radio is not in Idle/UplinkSync.
//
// Clock policy (ADR-003): authenticated requests need a plausible clock. When the clock is unknown
// the device first tries SNTP (bounded), then GET /v1/time over the validated TLS connection
// (backend_hint). If it is still unknown no authenticated request is sent and the state is ClockNotSet:
// that is a firmware POLICY gate, not a TLS result. The prebuilt ESP-IDF 4.4 mbedTLS for this core has
// MBEDTLS_HAVE_TIME_DATE disabled, so certificate validity dates are never checked by the stack and a TLS
// handshake/certificate failure can never be caused by the clock: it is always reported as CertError.
#include "backend_client.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/ssl.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <strings.h>

#include "app_events.h"
#include "backend_internal.h"
#include "device_config.h"
#include "evidence_store.h"
#include "net_link.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "radio_scheduler.h"
#include "time_sync.h"

#ifndef OYSTER_FIRMWARE_VERSION
#define OYSTER_FIRMWARE_VERSION "0.0.0-dev"
#endif

namespace oyster {

BackendClient g_backend;

const char* to_string(BackendState s) {
  switch (s) {
    case BackendState::Offline: return "offline";
    case BackendState::Connecting: return "connecting";
    case BackendState::ClockNotSet: return "clock_not_set";
    case BackendState::CertError: return "cert_error";
    case BackendState::AuthError: return "auth_error";
    case BackendState::Connected: return "connected";
  }
  return "offline";
}

namespace {

constexpr size_t kReqCap = cfg::kMaxApiPageBytes;          // request bodies
constexpr size_t kRespCap = cfg::kMaxApiPageBytes + 1024;  // response bodies (contract: < 4 KiB)
constexpr size_t kMaxCaBytes = 4096;
constexpr size_t kTokenCap = 160;
constexpr uint32_t kBackoffMinMs = 2000;
constexpr uint32_t kBackoffMaxMs = 60000;
constexpr uint32_t kAuthBackoffMs = 60000;
constexpr uint32_t kWifiWaitMs = 1500;
constexpr uint32_t kNtpWaitMs = 4000;
constexpr int kMaxBatchesPerCall = 8;
constexpr const char* kCapabilities[] = {"wifi_survey", "ble_discover", "ble_inspect_gatt", "ble_read_profile_value"};
constexpr const char* kEventsPrefix = "{\"events\":[";
constexpr const char* kEventStart = ",{\"schema_version\":";

// Negative "status" codes for transport failures.
constexpr int kErrOffline = -1;   // not provisioned, no link, paused or in backoff
constexpr int kErrIo = -3;        // write/read failure or timeout
constexpr int kErrProtocol = -4;  // malformed HTTP response or request too large
constexpr int kErrTooLarge = -5;  // response body over kRespCap (connection kept)
constexpr int kErrClock = -6;     // clock not plausible: no authenticated request sent
// A reused keep-alive connection idle longer than this is replaced before a request that must not be
// resent (the server closes idle connections after a few seconds).
constexpr uint32_t kReuseIdleMaxMs = 2000;

// Whether http() may transparently resend a request on a fresh connection when a reused keep-alive
// connection turns out to be dead before any response byte arrived.
enum class Retry : uint8_t { Once, Never };

// Exposes the protected last-error slot so a stale code from an earlier attempt is never reported.
class TlsClient : public WiFiClientSecure {
 public:
  void clear_last_error() { _lastError = 0; }
};

SemaphoreHandle_t s_mu = nullptr;
TlsClient* s_tls = nullptr;
char* s_ca = nullptr;  // kept alive: WiFiClientSecure keeps only the pointer
char s_token[kTokenCap];
bool s_have_token = false;
DeviceConfig s_cfg{};
std::atomic<uint8_t> s_state{static_cast<uint8_t>(BackendState::Offline)};
bool s_conn = false;
bool s_fresh = false;
uint32_t s_last_io_ms = 0;  // last completed response (or connect) on the current connection
std::atomic<bool> s_reload{false};  // invalidate_credentials(): re-read DeviceConfig before the next request
uint32_t s_backoff_ms = 0;
uint32_t s_next_attempt_ms = 0;
bool s_backoff_active = false;
char s_registered[cfg::kIdBufLen] = "";
char s_conflict[cfg::kIdBufLen] = "";
char s_last_error[128] = "";
int s_last_http = 0;
char s_last_code[32] = "";
bool s_last_submit_permanent = false;
char s_job_parse_error[64] = "";
uint32_t s_rejected_events = 0;
bool s_single_mode = false;
RunSummary s_runs[2];
std::atomic<uint8_t> s_run_idx{0};
char s_req[kReqCap];
char s_resp[kRespCap + 1];
size_t s_resp_len = 0;

class Lock {
 public:
  Lock() {
    if (s_mu == nullptr) s_mu = xSemaphoreCreateRecursiveMutex();
    xSemaphoreTakeRecursive(s_mu, portMAX_DELAY);
  }
  ~Lock() { xSemaphoreGiveRecursive(s_mu); }
  Lock(const Lock&) = delete;
  Lock& operator=(const Lock&) = delete;
};

void secure_zero(void* p, size_t n) {
  volatile uint8_t* v = static_cast<volatile uint8_t*>(p);
  while (n--) *v++ = 0;
}

void set_error(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(s_last_error, sizeof(s_last_error), fmt, ap);
  va_end(ap);
}

void set_state(BackendState st) {
  const uint8_t v = static_cast<uint8_t>(st);
  if (s_state.exchange(v) != v) {
    app_post(AppEventType::BackendStatusChanged, v);
    if (st != BackendState::Connected && st != BackendState::Connecting) log_w("backend: %s (%s)", to_string(st), s_last_error);
  }
}

BackendState state_now() { return static_cast<BackendState>(s_state.load()); }

void schedule_backoff(uint32_t min_ms) {
  s_backoff_ms = s_backoff_ms == 0 ? kBackoffMinMs : (s_backoff_ms * 2 > kBackoffMaxMs ? kBackoffMaxMs : s_backoff_ms * 2);
  if (s_backoff_ms < min_ms) s_backoff_ms = min_ms;
  s_next_attempt_ms = millis() + s_backoff_ms;
  s_backoff_active = true;
}

void clear_backoff() {
  s_backoff_ms = 0;
  s_backoff_active = false;
}

bool backoff_due() { return !s_backoff_active || static_cast<int32_t>(millis() - s_next_attempt_ms) >= 0; }

void close_conn() {
  if (s_tls != nullptr) s_tls->stop();
  s_conn = false;
  s_fresh = false;
}

bool is_cert_error(int err) {
  if (err >= 0) return false;
  const int high = (-err) & 0x7F80;
  if (high >= 0x2080 && high <= 0x3000) return true;  // X.509 (verify failed, bad certificate, ...)
  if (high >= 0x1080 && high <= 0x1480) return true;  // PEM: the provisioned CA could not be parsed
  return high == ((-MBEDTLS_ERR_SSL_CA_CHAIN_REQUIRED) & 0x7F80) ||
         high == ((-MBEDTLS_ERR_SSL_PEER_VERIFY_FAILED) & 0x7F80) ||
         high == ((-MBEDTLS_ERR_SSL_BAD_HS_CERTIFICATE) & 0x7F80);
}

bool provisioned() { return s_cfg.provisioned && s_have_token && s_ca != nullptr && s_tls != nullptr; }

// Reads DeviceConfig, the device token and (when not cached) the backend CA. Caller holds the lock.
void load_credentials_locked() {
  g_config.load(s_cfg);
  secure_zero(s_token, sizeof(s_token));
  s_have_token = g_config.get_device_token(s_token, sizeof(s_token)) && s_token[0] != '\0';
  if (s_ca == nullptr) {
    const size_t ca_len = g_config.ca_pem_len();
    if (ca_len > 0 && ca_len <= kMaxCaBytes) {
      s_ca = static_cast<char*>(malloc(ca_len + 1));
      if (s_ca != nullptr && !g_config.get_ca_pem(s_ca, ca_len + 1)) {
        free(s_ca);
        s_ca = nullptr;
      }
    }
  }
}

// invalidate_credentials() ran (FACTORY-RESET, DONE, re-provisioning): forget the token, the CA pointer, the
// connection and everything bound to the old identity, then read the configuration again.
void reload_credentials_locked() {
  close_conn();
  clear_backoff();
  free(s_ca);
  s_ca = nullptr;
  s_registered[0] = '\0';
  s_conflict[0] = '\0';
  s_single_mode = false;
  load_credentials_locked();
  if (!provisioned()) set_error("device not provisioned (serial setup required)");
}

// ---- connection -----------------------------------------------------------------------------------

bool ensure_connected() {
  if (s_reload.exchange(false)) reload_credentials_locked();
  if (!provisioned()) {
    set_error("device not provisioned (serial setup required)");
    set_state(BackendState::Offline);
    return false;
  }
  if (!g_radio.uplink_allowed()) return false;  // paused: radio busy with a survey or BLE session
  if (s_conn && s_tls->connected()) return true;
  if (s_conn) close_conn();
  if (!backoff_due()) return false;
  if (!net::ensure_up(kWifiWaitMs)) {
    set_error("Wi-Fi not connected");
    set_state(BackendState::Offline);
    return false;
  }
  if (!timesync::plausible()) timesync::wait_ntp(kNtpWaitMs);
  set_state(BackendState::Connecting);
  s_tls->clear_last_error();
  s_tls->setCACert(s_ca);
  s_tls->setHandshakeTimeout(cfg::kTlsHandshakeTimeoutS);
  const int ok = s_tls->connect(s_cfg.backend_host, s_cfg.backend_port, static_cast<int32_t>(cfg::kHttpTimeoutMs));
  if (!ok) {
    char text[80] = "";
    const int err = s_tls->lastError(text, sizeof(text));
    s_tls->stop();
    if (is_cert_error(err)) {
      // Never ClockNotSet: this stack does not check certificate dates, so the clock cannot cause this.
      set_error("TLS certificate rejected (%d: %s); check the CA and that the certificate names %s", err, text,
                s_cfg.backend_host);
      set_state(BackendState::CertError);
      schedule_backoff(kAuthBackoffMs);
    } else {
      if (err != 0) {
        set_error("backend unreachable: TLS error %d (%s)", err, text);
      } else {
        set_error("backend unreachable: DNS, TCP connect or TLS handshake timeout");
      }
      set_state(BackendState::Offline);
      schedule_backoff(0);
    }
    return false;
  }
  s_tls->setTimeout(cfg::kHttpTimeoutMs / 1000);  // seconds (socket send/receive timeouts)
  s_conn = true;
  s_fresh = true;
  s_last_io_ms = millis();
  clear_backoff();
  return true;
}

// ---- bounded HTTP/1.1 -----------------------------------------------------------------------------

class Rx {
 public:
  Rx(TlsClient& c, uint32_t timeout_ms) : c_(c), start_(millis()), timeout_(timeout_ms) {}

  int get() {
    if (pos_ < len_) return buf_[pos_++];
    for (;;) {
      const int a = c_.available();
      if (a > 0) {
        const int want = a < static_cast<int>(sizeof(buf_)) ? a : static_cast<int>(sizeof(buf_));
        const int r = c_.read(buf_, static_cast<size_t>(want));
        if (r > 0) {
          len_ = static_cast<size_t>(r);
          pos_ = 1;
          got_ += static_cast<uint32_t>(r);
          return buf_[0];
        }
      }
      if (a < 0 || !c_.connected()) return -1;
      if (millis() - start_ >= timeout_) return -1;
      vTaskDelay(pdMS_TO_TICKS(5));
    }
  }

  // One line without CR/LF; longer lines are truncated (the rest is consumed).
  bool line(char* out, size_t cap, size_t& len) {
    len = 0;
    for (;;) {
      const int c = get();
      if (c < 0) return false;
      if (c == '\n') break;
      if (c == '\r') continue;
      if (len + 1 < cap) out[len++] = static_cast<char>(c);
    }
    out[len] = '\0';
    return true;
  }

  uint32_t got() const { return got_; }

 private:
  TlsClient& c_;
  uint32_t start_;
  uint32_t timeout_;
  uint8_t buf_[256];
  size_t pos_ = 0;
  size_t len_ = 0;
  uint32_t got_ = 0;
};

bool header_is(const char* line, const char* name, const char*& value) {
  const size_t n = std::strlen(name);
  if (strncasecmp(line, name, n) != 0 || line[n] != ':') return false;
  value = line + n + 1;
  while (*value == ' ' || *value == '\t') ++value;
  return true;
}

bool contains_ci(const char* hay, const char* needle) {
  const size_t n = std::strlen(needle);
  for (const char* p = hay; *p != '\0'; ++p) {
    if (strncasecmp(p, needle, n) == 0) return true;
  }
  return false;
}

int http_once(const char* method, const char* path, const char* body, size_t body_len, bool auth, uint32_t& got) {
  got = 0;
  char head[448];
  int n = snprintf(head, sizeof(head),
                   "%s %s HTTP/1.1\r\nHost: %s:%u\r\nUser-Agent: oyster-pocket/%s\r\nAccept: application/json\r\n"
                   "Connection: keep-alive\r\n",
                   method, path, s_cfg.backend_host, static_cast<unsigned>(s_cfg.backend_port), OYSTER_FIRMWARE_VERSION);
  if (n > 0 && auth && static_cast<size_t>(n) < sizeof(head)) {
    n += snprintf(head + n, sizeof(head) - n, "Authorization: Bearer %s\r\n", s_token);
  }
  if (n > 0 && static_cast<size_t>(n) < sizeof(head)) {
    if (body != nullptr) {
      n += snprintf(head + n, sizeof(head) - n, "Content-Type: application/json\r\nContent-Length: %u\r\n\r\n",
                    static_cast<unsigned>(body_len));
    } else if (std::strcmp(method, "GET") != 0) {
      n += snprintf(head + n, sizeof(head) - n, "Content-Length: 0\r\n\r\n");
    } else {
      n += snprintf(head + n, sizeof(head) - n, "\r\n");
    }
  }
  if (n <= 0 || static_cast<size_t>(n) >= sizeof(head)) {
    secure_zero(head, sizeof(head));
    return kErrProtocol;
  }
  const size_t w = s_tls->write(reinterpret_cast<const uint8_t*>(head), static_cast<size_t>(n));
  secure_zero(head, sizeof(head));  // carried the bearer token
  if (w != static_cast<size_t>(n)) return kErrIo;
  if (body != nullptr && body_len > 0 && s_tls->write(reinterpret_cast<const uint8_t*>(body), body_len) != body_len) {
    return kErrIo;
  }

  Rx rx(*s_tls, cfg::kHttpTimeoutMs);
  char line[160];
  size_t len = 0;
  if (!rx.line(line, sizeof(line), len)) {
    got = rx.got();
    return kErrIo;
  }
  int status = 0;
  if (std::strncmp(line, "HTTP/1.", 7) != 0 || len < 12 || sscanf(line + 9, "%3d", &status) != 1 || status < 100) {
    got = rx.got();
    return kErrProtocol;
  }
  bool close_after = std::strncmp(line, "HTTP/1.0", 8) == 0;
  bool chunked = false;
  long content_len = -1;
  for (;;) {
    if (!rx.line(line, sizeof(line), len)) {
      got = rx.got();
      return kErrIo;
    }
    if (len == 0) break;
    const char* v = nullptr;
    if (header_is(line, "content-length", v)) {
      content_len = strtol(v, nullptr, 10);
    } else if (header_is(line, "transfer-encoding", v)) {
      chunked = contains_ci(v, "chunked");
    } else if (header_is(line, "connection", v)) {
      if (contains_ci(v, "close")) close_after = true;
      if (contains_ci(v, "keep-alive")) close_after = false;
    }
  }

  s_resp_len = 0;
  bool too_large = false;
  bool body_ok = true;
  auto store = [&](int c) {
    if (s_resp_len < kRespCap) {
      s_resp[s_resp_len++] = static_cast<char>(c);
    } else {
      too_large = true;
    }
  };
  if (status == 204 || status == 304 || status < 200) {
    // no body
  } else if (chunked) {
    for (;;) {
      if (!rx.line(line, sizeof(line), len)) {
        body_ok = false;
        break;
      }
      const unsigned long sz = strtoul(line, nullptr, 16);
      if (sz == 0) {
        while (rx.line(line, sizeof(line), len) && len > 0) {
        }
        break;
      }
      for (unsigned long i = 0; i < sz && body_ok; ++i) {
        const int c = rx.get();
        if (c < 0) {
          body_ok = false;
        } else {
          store(c);
        }
      }
      if (!body_ok || !rx.line(line, sizeof(line), len)) {
        body_ok = false;
        break;
      }
    }
  } else if (content_len >= 0) {
    for (long i = 0; i < content_len; ++i) {
      const int c = rx.get();
      if (c < 0) {
        body_ok = false;
        break;
      }
      store(c);
    }
  } else {
    for (;;) {
      const int c = rx.get();
      if (c < 0) break;
      store(c);
    }
    close_after = true;
  }
  s_resp[s_resp_len] = '\0';
  got = rx.got();
  s_last_io_ms = millis();
  if (!body_ok) return kErrIo;
  if (close_after) close_conn();
  if (too_large) {
    s_resp_len = 0;
    s_resp[0] = '\0';
    return kErrTooLarge;
  }
  return status;
}

void parse_error_code() {
  s_last_code[0] = '\0';
  if (s_resp_len == 0) return;
  JsonDocument doc;
  if (deserializeJson(doc, s_resp, s_resp_len) != DeserializationError::Ok) return;
  const char* code = doc["error"].as<const char*>();
  if (code == nullptr) code = doc["detail"]["error"].as<const char*>();  // FastAPI HTTPException(detail={...})
  if (code != nullptr) copy_bounded(s_last_code, sizeof(s_last_code), code);
}

bool ensure_clock();

// One request. Retry::Once allows one transparent resend when a reused keep-alive connection turns out to
// be dead before any response byte arrived (the server closed it while idle); only for requests that are
// safe to repeat (GETs, event upload, results, status reports, session registration, run cancel).
// Retry::Never is for requests that are not idempotent (creating an AI run): a request that may already have
// reached the server is never resent. To keep that first attempt reliable, a connection that has been idle
// for kReuseIdleMaxMs is replaced by a fresh one before the request is sent.
int http(const char* method, const char* path, const char* body, size_t body_len, bool auth,
         Retry retry = Retry::Once) {
  s_resp_len = 0;
  s_resp[0] = '\0';
  s_last_code[0] = '\0';
  for (int attempt = 0; attempt < 2; ++attempt) {
    if (retry == Retry::Never && s_conn && !s_fresh && millis() - s_last_io_ms > kReuseIdleMaxMs) close_conn();
    if (!ensure_connected()) {
      s_last_http = kErrOffline;
      return kErrOffline;
    }
    if (auth && !ensure_clock()) {
      s_last_http = kErrClock;
      return kErrClock;
    }
    const bool reused = !s_fresh;
    s_fresh = false;
    uint32_t got = 0;
    const int st = http_once(method, path, body, body_len, auth, got);
    s_last_http = st;
    if (st == kErrTooLarge) {
      set_error("response from %s larger than %u bytes", path, static_cast<unsigned>(kRespCap));
      return st;
    }
    if (st > 0) {
      if (st == 401 || st == 403) {
        set_error("backend rejected the device token (HTTP %d); re-enrol and re-provision", st);
        set_state(BackendState::AuthError);
        close_conn();
        schedule_backoff(kAuthBackoffMs);
      } else if (auth) {
        set_state(BackendState::Connected);
        if (st < 400) s_last_error[0] = '\0';
      }
      if (st >= 400) parse_error_code();
      return st;
    }
    close_conn();
    if (reused && got == 0 && attempt == 0 && retry == Retry::Once) continue;
    set_error("request %s %s failed (transport error %d)", method, path, st);
    set_state(BackendState::Offline);
    schedule_backoff(0);
    return st;
  }
  return kErrIo;
}

bool fetch_time_locked(uint64_t& unix_seconds) {
  const int st = http("GET", "/v1/time", nullptr, 0, false);
  if (st != 200) return false;
  JsonDocument doc;
  if (deserializeJson(doc, s_resp, s_resp_len) != DeserializationError::Ok) return false;
  JsonVariantConst u = doc["unix"];
  if (!u.is<uint64_t>()) return false;
  unix_seconds = u.as<uint64_t>();
  timesync::apply_backend_hint(unix_seconds);
  return true;
}

bool ensure_clock() {
  if (timesync::plausible()) return true;
  uint64_t t = 0;
  fetch_time_locked(t);
  if (timesync::plausible()) return true;
  set_error("clock not set (firmware policy): no NTP, serial seed or backend time hint; no authenticated request sent");
  set_state(BackendState::ClockNotSet);
  return false;
}

bool ok_status(int st) { return st == 200 || st == 201 || st == 202; }

const char* find_bytes(const char* hay, size_t n, const char* needle) {
  const size_t k = std::strlen(needle);
  for (size_t i = 0; k <= n && i <= n - k; ++i) {
    if (std::memcmp(hay + i, needle, k) == 0) return hay + i;
  }
  return nullptr;
}

// ---- parsing --------------------------------------------------------------------------------------

void copy_excerpt(char* out, size_t cap, const char* in) {
  // Control characters become spaces; the cut never splits a UTF-8 sequence.
  size_t n = 0;
  if (cap == 0) return;
  for (const char* p = in; p != nullptr && *p != '\0' && n + 1 < cap; ++p) {
    const unsigned char c = static_cast<unsigned char>(*p);
    out[n++] = c < 0x20 ? ' ' : static_cast<char>(c);
  }
  out[n] = '\0';
  if (in != nullptr && std::strlen(in) > n) {
    while (n > 0 && (static_cast<unsigned char>(out[n - 1]) & 0xC0) == 0x80) --n;  // continuation bytes
    if (n > 0 && (static_cast<unsigned char>(out[n - 1]) & 0xC0) == 0xC0) --n;     // dangling lead byte
    out[n] = '\0';
  }
}

void publish_run(const RunSummary& r) {
  const uint8_t next = static_cast<uint8_t>(1 - s_run_idx.load());
  s_runs[next] = r;
  s_run_idx.store(next);
}

bool parse_run(RunSummary& out) {
  JsonDocument filter;
  filter["run_id"] = true;
  filter["status"] = true;
  filter["error"] = true;
  filter["answer"]["answer_markdown"] = true;
  filter["pending_action"] = true;
  JsonDocument doc;
  if (deserializeJson(doc, s_resp, s_resp_len, DeserializationOption::Filter(filter)) != DeserializationError::Ok) {
    return false;
  }
  std::memset(&out, 0, sizeof(out));
  const char* rid = doc["run_id"].as<const char*>();
  const char* st = doc["status"].as<const char*>();
  if (rid == nullptr || !is_prefixed_id(rid, "run-") || st == nullptr) return false;
  copy_bounded(out.run_id, sizeof(out.run_id), rid);
  copy_bounded(out.status, sizeof(out.status), st);
  const char* ans = doc["answer"]["answer_markdown"].as<const char*>();
  const char* err = doc["error"].as<const char*>();
  if (ans != nullptr) {
    copy_excerpt(out.answer_excerpt, sizeof(out.answer_excerpt), ans);
  } else if (err != nullptr) {
    char tmp[sizeof(out.answer_excerpt)];
    snprintf(tmp, sizeof(tmp), "error: %s", err);
    copy_excerpt(out.answer_excerpt, sizeof(out.answer_excerpt), tmp);
  }
  JsonVariantConst pa = doc["pending_action"];
  if (pa.is<JsonObjectConst>()) {
    out.has_pending_action = true;
    const char* op = pa["operation"].as<const char*>();
    const char* tg = pa["target_id"].as<const char*>();
    const char* fx = pa["expected_effect"].as<const char*>();
    if (op != nullptr) copy_excerpt(out.pending_operation, sizeof(out.pending_operation), op);
    if (tg != nullptr) copy_excerpt(out.pending_target, sizeof(out.pending_target), tg);
    if (fx != nullptr) copy_excerpt(out.pending_effect, sizeof(out.pending_effect), fx);
  }
  return true;
}

bool is_job_key(const char* k) {
  static const char* const kKeys[] = {"schema_version", "job_id", "session_id", "device_id", "target_id",
                                      "operation", "arguments", "scope_version", "deadline_s", "limits",
                                      "requires_local_confirmation", "nonce"};
  for (const char* key : kKeys) {
    if (std::strcmp(k, key) == 0) return true;
  }
  return false;
}

// Optional string or null into a bounded field. False when the value has another type or is too long.
bool opt_string(JsonVariantConst v, char* out, size_t cap) {
  out[0] = '\0';
  if (v.isNull()) return true;
  const char* s = v.as<const char*>();
  if (s == nullptr || std::strlen(s) >= cap) return false;
  copy_bounded(out, cap, s);
  return true;
}

// Fills `out` from the first job of a GET /v1/device/jobs body. Schema problems do not drop the job:
// they are reported through job_parse_error() and leave session_id empty, so LocalPolicy rejects it
// as malformed and the result says why.
bool parse_job(JobSpec& out) {
  s_job_parse_error[0] = '\0';
  JsonDocument doc;
  if (deserializeJson(doc, s_resp, s_resp_len) != DeserializationError::Ok) {
    set_error("jobs response is not valid JSON");
    return false;
  }
  JsonVariantConst jobs = doc["jobs"];
  if (!jobs.is<JsonArrayConst>()) {
    set_error("jobs response has no jobs array");
    return false;
  }
  JsonArrayConst arr = jobs.as<JsonArrayConst>();
  if (arr.size() == 0) return false;
  JsonVariantConst jv = arr[0];
  if (!jv.is<JsonObjectConst>()) return false;
  JsonObjectConst j = jv.as<JsonObjectConst>();

  std::memset(&out, 0, sizeof(out));
  out.op = Operation::Unknown;
  out.received_ms = timesync::uptime_ms();
  const char* job_id = j["job_id"].as<const char*>();
  if (job_id == nullptr || !is_valid_job_id(job_id)) {
    set_error("delivered job without a valid job_id ignored");
    log_w("backend: delivered job without a valid job_id ignored");
    return false;
  }
  copy_bounded(out.job_id, sizeof(out.job_id), job_id);

  const char* perr = nullptr;
  for (JsonPairConst kv : j) {
    if (!is_job_key(kv.key().c_str())) perr = "unknown field";
  }
  const char* sv = j["schema_version"].as<const char*>();
  if (perr == nullptr && (sv == nullptr || std::strcmp(sv, "1.0") != 0)) perr = "schema_version";
  const char* sid = j["session_id"].as<const char*>();
  if (sid != nullptr && std::strlen(sid) < sizeof(out.session_id)) copy_bounded(out.session_id, sizeof(out.session_id), sid);
  if (perr == nullptr && (sid == nullptr || !is_valid_session_id(sid))) perr = "session_id";
  if (!opt_string(j["device_id"], out.device_id, sizeof(out.device_id)) && perr == nullptr) perr = "device_id";
  if (!opt_string(j["target_id"], out.target_id, sizeof(out.target_id)) && perr == nullptr) perr = "target_id";
  const char* op = j["operation"].as<const char*>();
  if (op != nullptr) {
    copy_bounded(out.op_wire, sizeof(out.op_wire), op);
    Operation parsed = Operation::Unknown;
    if (parse_operation(op, parsed)) out.op = parsed;  // unknown names -> LocalPolicy: unknown operation
  } else if (perr == nullptr) {
    perr = "operation";
  }
  JsonVariantConst args = j["arguments"];
  if (args.is<JsonObjectConst>()) {
    const size_t m = measureJson(args);
    if (m < sizeof(out.args_json)) {
      serializeJson(args, out.args_json, sizeof(out.args_json));
      out.args_len = static_cast<uint16_t>(m);
    } else {
      out.args_json[0] = '\0';
      const size_t over = m > cfg::kMaxJobArgsBytes ? m : cfg::kMaxJobArgsBytes + 1;
      out.args_len = static_cast<uint16_t>(over > UINT16_MAX ? UINT16_MAX : over);  // LocalPolicy rejects
    }
  } else if (perr == nullptr) {
    perr = "arguments";
  }
  JsonVariantConst scope = j["scope_version"];
  if (scope.is<uint32_t>() && scope.as<uint32_t>() >= 1) {
    out.scope_version = scope.as<uint32_t>();
  } else if (perr == nullptr) {
    perr = "scope_version";
  }
  JsonVariantConst dl = j["deadline_s"];
  if (dl.is<float>() && dl.as<float>() > 0.0f && dl.as<float>() <= 600.0f) {
    out.deadline_ms = static_cast<uint32_t>(dl.as<float>() * 1000.0f + 0.5f);
  } else if (perr == nullptr) {
    perr = "deadline_s";
  }
  JsonVariantConst lim = j["limits"];
  if (lim.is<JsonObjectConst>()) {
    for (JsonPairConst kv : lim.as<JsonObjectConst>()) {
      const char* k = kv.key().c_str();
      JsonVariantConst v = kv.value();
      if (std::strcmp(k, "max_results") == 0 && v.is<uint32_t>() && v.as<uint32_t>() >= 1 && v.as<uint32_t>() <= 256) {
        out.limits.max_results = static_cast<uint16_t>(v.as<uint32_t>());
      } else if (std::strcmp(k, "max_duration_s") == 0 && v.is<float>() && v.as<float>() > 0.0f &&
                 v.as<float>() <= 600.0f) {
        out.limits.max_duration_ms = static_cast<uint32_t>(v.as<float>() * 1000.0f + 0.5f);
      } else if (std::strcmp(k, "max_bytes") == 0 && v.is<uint32_t>() && v.as<uint32_t>() >= 1 &&
                 v.as<uint32_t>() <= 65536) {
        out.limits.max_bytes = v.as<uint32_t>();
      } else if (perr == nullptr) {
        perr = "limits";
      }
    }
  } else if (perr == nullptr) {
    perr = "limits";
  }
  JsonVariantConst rlc = j["requires_local_confirmation"];
  if (rlc.is<bool>()) {
    out.server_requires_confirmation = rlc.as<bool>();
  } else if (perr == nullptr) {
    perr = "requires_local_confirmation";
  }
  if (!opt_string(j["nonce"], out.nonce, sizeof(out.nonce)) && perr == nullptr) perr = "nonce";

  if (perr != nullptr) {
    snprintf(s_job_parse_error, sizeof(s_job_parse_error), "malformed job field: %s", perr);
    out.session_id[0] = '\0';  // LocalPolicy -> RejectMalformed
  }
  return true;
}

bool build_result(const char* job_id, JobOutcome outcome, const char* const* event_ids, size_t n_events,
                  const char* error, const LocalConfirmation* confirmation, size_t& len) {
  JsonWriter w(s_req, kReqCap);
  w.begin_object();
  w.str("schema_version", "1.0");
  w.str("job_id", job_id);
  char key[96];
  snprintf(key, sizeof(key), "res-%s-%s", job_id, g_evidence.boot_id());
  w.str("idempotency_key", key);
  w.str("outcome", to_string(outcome));
  w.begin_array("event_ids");
  for (size_t i = 0; i < n_events && i < 256; ++i) {
    if (event_ids != nullptr && event_ids[i] != nullptr && event_ids[i][0] != '\0') w.str_value(event_ids[i]);
  }
  w.end_array();
  w.begin_array("artifact_ids");
  w.end_array();
  if (error != nullptr && error[0] != '\0') {
    char e[241];
    copy_bounded(e, sizeof(e), error);  // schema maxLength 240; truncation is acceptable here
    w.str("error", e);
  } else {
    w.null("error");
  }
  if (confirmation != nullptr) {
    w.begin_object("confirmation");
    w.str("job_id", confirmation->job_id);
    w.str("nonce", confirmation->nonce);
    w.boolean("approved", confirmation->approved);
    w.end_object();
  } else {
    w.null("confirmation");
  }
  w.end_object();
  len = w.size();
  return w.ok();
}

bool register_locked(const char* session_id) {
  if (!is_valid_session_id(session_id)) return false;
  if (std::strcmp(s_registered, session_id) == 0) return true;
  if (std::strcmp(s_conflict, session_id) == 0) return false;  // owned by another device: never retried
  JsonWriter w(s_req, kReqCap);
  w.begin_object();
  w.str("session_id", session_id);
  w.str("source_mode", "live");
  w.begin_array("capabilities");
  for (const char* cap : kCapabilities) w.str_value(cap);
  w.end_array();
  w.end_object();
  if (!w.ok()) return false;
  const int st = http("POST", "/v1/sessions", s_req, w.size(), true);
  if (ok_status(st)) {
    copy_bounded(s_registered, sizeof(s_registered), session_id);
    return true;
  }
  if (st == 409) {
    copy_bounded(s_conflict, sizeof(s_conflict), session_id);
    set_error("session %s is owned by another device; open a new session", session_id);
    app_post(AppEventType::Toast, 0, 0, "Session id conflict: open a new session");
  }
  return false;
}

bool upload_locked() {
  // One snapshot of the session id serves the URL AND every batch: read_batch()/ack_through()/skip_event() are
  // given the same id, so a session switch by the UI cannot send session B's events to session A's path.
  char sid[cfg::kIdBufLen];
  copy_bounded(sid, sizeof(sid), g_evidence.session_id());
  if (sid[0] == '\0') return true;  // nothing to upload
  if (!register_locked(sid)) return false;
  char path[96];
  snprintf(path, sizeof(path), "/v1/sessions/%s/events", sid);
  const size_t pre = std::strlen(kEventsPrefix);
  for (int i = 0; i < kMaxBatchesPerCall; ++i) {
    if (!g_radio.uplink_allowed()) return false;
    std::memcpy(s_req, kEventsPrefix, pre);
    uint32_t first = 0;
    uint32_t last = 0;
    uint16_t count = 0;
    size_t n = g_evidence.read_batch(sid, s_req + pre, kReqCap - pre - 2, first, last, count);
    if (count == 0) return true;
    if (s_single_mode && count > 1) {
      // Isolating an event the backend refuses: send the first event alone.
      const char* hit = find_bytes(s_req + pre, n, kEventStart);
      if (hit != nullptr) {
        n = static_cast<size_t>(hit - (s_req + pre));
        last = first;
        count = 1;
      }
    }
    s_req[pre + n] = ']';
    s_req[pre + n + 1] = '}';
    const int st = http("POST", path, s_req, pre + n + 2, true);
    if (ok_status(st)) {
      g_evidence.ack_through(sid, first, last);
      continue;
    }
    if (st == 404) {
      s_registered[0] = '\0';  // unknown to the backend (e.g. backend data reset): register again
      return false;
    }
    if (st == 400 || st == 409 || st == 413 || st == 422) {
      if (count > 1) {
        s_single_mode = true;
        continue;
      }
      // A single event the backend refuses: skip it so the queue cannot stall. It stays on the card but is NOT
      // acknowledged: it is counted in skipped_events() and can never be cited by a job result.
      ++s_rejected_events;
      log_w("backend: event seq %lu refused (HTTP %d %s); skipped, not acknowledged", static_cast<unsigned long>(first),
            st, s_last_code);
      g_evidence.skip_event(sid, first);
      s_single_mode = false;
      continue;
    }
    return false;
  }
  return false;  // more remain; the next cycle continues
}

}  // namespace

// ---- public API -----------------------------------------------------------------------------------

bool BackendClient::begin() {
  Lock l;
  load_credentials_locked();
  if (s_tls == nullptr) s_tls = new (std::nothrow) TlsClient();
  RunSummary empty{};
  publish_run(empty);
  if (!provisioned()) set_error("device not provisioned (serial setup required)");
  s_state.store(static_cast<uint8_t>(BackendState::Offline));
  return s_tls != nullptr;
}

BackendState BackendClient::state() const { return state_now(); }

// Called by the provisioning code (any task). The worker owns the connection and the token buffer, so this only
// raises a flag: the next request re-reads DeviceConfig (token, CA, host) on a fresh connection. The state drops
// to Offline now so the UI does not keep showing "connected" for credentials that were just removed.
void BackendClient::invalidate_credentials() {
  s_reload.store(true);
  set_state(BackendState::Offline);
}

bool BackendClient::poll_job(JobSpec& out) {
  Lock l;
  s_job_parse_error[0] = '\0';
  const int st = http("GET", "/v1/device/jobs", nullptr, 0, true);
  if (st != 200) return false;
  return parse_job(out);
}

bool BackendClient::submit_result(const char* job_id, JobOutcome outcome, const char* const* event_ids, size_t n_events,
                                  const char* error, const LocalConfirmation* confirmation) {
  Lock l;
  s_last_submit_permanent = false;
  if (job_id == nullptr || !is_valid_job_id(job_id)) {
    s_last_submit_permanent = true;
    return false;
  }
  char path[96];
  snprintf(path, sizeof(path), "/v1/jobs/%s/result", job_id);
  int st = 0;
  for (int attempt = 0; attempt < 2; ++attempt) {
    size_t len = 0;
    if (!build_result(job_id, outcome, event_ids, n_events, error, confirmation, len)) {
      s_last_submit_permanent = true;
      return false;
    }
    st = http("POST", path, s_req, len, true);
    if (ok_status(st)) return true;
    if (st == 409 && std::strcmp(s_last_code, "events_missing") == 0 && attempt == 0) {
      upload_locked();  // results never precede their evidence; then retry once
      continue;
    }
    break;
  }
  // 4xx other than auth, timeout, rate limit and events_missing will not change on retry.
  s_last_submit_permanent = st >= 400 && st < 500 && st != 401 && st != 403 && st != 408 && st != 429 &&
                            !(st == 409 && std::strcmp(s_last_code, "events_missing") == 0);
  return false;
}

bool BackendClient::upload_pending_events() {
  Lock l;
  return upload_locked();
}

bool BackendClient::report_job_status(const char* job_id, const char* status) {
  Lock l;
  if (job_id == nullptr || !is_valid_job_id(job_id) || status == nullptr) return false;
  char path[96];
  snprintf(path, sizeof(path), "/v1/jobs/%s/status", job_id);
  char body[96];
  JsonWriter w(body, sizeof(body));
  w.begin_object();
  w.str("status", status);
  w.end_object();
  if (!w.ok()) return false;
  return ok_status(http("POST", path, body, w.size(), true));
}

bool BackendClient::start_run(const char* session_id, const char* question, char out_run_id[cfg::kIdBufLen]) {
  Lock l;
  if (out_run_id != nullptr) out_run_id[0] = '\0';
  if (session_id == nullptr || !is_valid_session_id(session_id) || question == nullptr || question[0] == '\0') return false;
  if (!register_locked(session_id)) return false;
  JsonWriter w(s_req, kReqCap);
  w.begin_object();
  w.str("question", question);
  w.end_object();
  if (!w.ok()) return false;
  char path[112];
  snprintf(path, sizeof(path), "/v1/sessions/%s/assistant/messages", session_id);
  // Creating a run is not idempotent: never resent after a dead keep-alive connection (no duplicate AI runs).
  const int st = http("POST", path, s_req, w.size(), true, Retry::Never);
  if (!ok_status(st)) return false;
  JsonDocument doc;
  if (deserializeJson(doc, s_resp, s_resp_len) != DeserializationError::Ok) return false;
  const char* rid = doc["run_id"].as<const char*>();
  if (rid == nullptr || !is_prefixed_id(rid, "run-")) return false;
  RunSummary r{};
  copy_bounded(r.run_id, sizeof(r.run_id), rid);
  const char* status = doc["status"].as<const char*>();
  copy_bounded(r.status, sizeof(r.status), status != nullptr ? status : "queued");
  publish_run(r);
  if (out_run_id != nullptr) copy_bounded(out_run_id, cfg::kIdBufLen, rid);
  return true;
}

bool BackendClient::poll_run(const char* run_id, RunSummary& out) {
  Lock l;
  if (run_id == nullptr || !is_prefixed_id(run_id, "run-")) return false;
  char path[96];
  snprintf(path, sizeof(path), "/v1/runs/%s", run_id);
  if (http("GET", path, nullptr, 0, true) != 200) return false;
  if (!parse_run(out)) {
    set_error("run status response could not be parsed");
    return false;
  }
  publish_run(out);
  return true;
}

int BackendClient::call(const char* method, const char* path, const char* body, size_t body_len, char* resp,
                        size_t cap, size_t* resp_len, bool idempotent) {
  Lock l;
  if (resp_len != nullptr) *resp_len = 0;
  if (resp != nullptr && cap > 0) resp[0] = '\0';
  if (method == nullptr || path == nullptr || std::strncmp(path, "/v1/", 4) != 0) return kErrIo;
  if (body_len > kReqCap) return kErrTooLarge;
  const int st = http(method, path, body, body_len, true, idempotent ? Retry::Once : Retry::Never);
  if (resp != nullptr && cap > 0 && s_resp_len > 0) {
    const size_t n = s_resp_len < cap - 1 ? s_resp_len : cap - 1;
    std::memcpy(resp, s_resp, n);
    resp[n] = '\0';
    if (resp_len != nullptr) *resp_len = n;
  }
  return st;
}

bool BackendClient::cancel_run(const char* run_id) {
  Lock l;
  if (run_id == nullptr || !is_prefixed_id(run_id, "run-")) return false;
  char path[96];
  snprintf(path, sizeof(path), "/v1/runs/%s/cancel", run_id);
  return ok_status(http("POST", path, nullptr, 0, true));
}

bool BackendClient::register_session(const char* session_id) {
  Lock l;
  return register_locked(session_id);
}

bool BackendClient::fetch_time(uint64_t& unix_seconds) {
  Lock l;
  return fetch_time_locked(unix_seconds);
}

const RunSummary& BackendClient::latest_run() const { return s_runs[s_run_idx.load()]; }

// ---- diagnostics ----------------------------------------------------------------------------------

namespace backend_detail {

const char* last_error() { return s_last_error; }
int last_http_status() { return s_last_http; }
const char* last_error_code() { return s_last_code; }
bool last_submit_permanent() { return s_last_submit_permanent; }
const char* job_parse_error() { return s_job_parse_error; }
uint32_t rejected_events() { return s_rejected_events; }
bool session_registered(const char* session_id) {
  return session_id != nullptr && session_id[0] != '\0' && std::strcmp(s_registered, session_id) == 0;
}
bool session_conflict() {
  const char* sid = g_evidence.session_id();
  return sid != nullptr && sid[0] != '\0' && std::strcmp(s_conflict, sid) == 0;
}

}  // namespace backend_detail

}  // namespace oyster
