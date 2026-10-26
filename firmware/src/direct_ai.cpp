// Direct-AI client: one single-turn chat request to an OpenAI-compatible endpoint over validated TLS.
// See direct_ai.h for the contract. The wire format (body builder, reply parser) lives in
// oyster_core/directai_parse.h and is host-tested; this file is the device transport only and compiles
// against the Arduino core, so it is not part of the native test build.
#include "direct_ai.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <freertos/FreeRTOS.h>
#include <mbedtls/ssl.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <new>

#include "groq_ca.h"
#include "oyster_core/config.h"
#include "oyster_core/directai_parse.h"

#ifndef OYSTER_FIRMWARE_VERSION
#define OYSTER_FIRMWARE_VERSION "0.0.0-dev"
#endif

namespace oyster::directai {

namespace {

constexpr size_t kBodyCap = 4096;          // request body (question + system prompt)
constexpr size_t kRespCap = 6144;          // reply body cap (contract: a few KiB)
constexpr uint32_t kDefaultTimeoutMs = 20000;

// Called from the worker task only (one network owner), so static scratch is safe and keeps the 16 KiB
// worker stack clear of large buffers.
char s_body[kBodyCap];
char s_resp[kRespCap + 1];

void secure_zero(void* p, size_t n) {
  volatile uint8_t* v = static_cast<volatile uint8_t*>(p);
  while (n-- != 0) *v++ = 0;
}

void set_err(char* out, size_t cap, const char* fmt, ...) {
  if (out == nullptr || cap == 0) return;
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(out, cap, fmt, ap);
  va_end(ap);
}

// Defensive: strip any occurrence of the key from text that a caller will see. The key is only ever
// written to the Authorization header (zeroed after sending), so this should find nothing.
void scrub_key(char* text, const char* key) {
  if (text == nullptr || key == nullptr || key[0] == '\0') return;
  const size_t kl = std::strlen(key);
  for (char* p = std::strstr(text, key); p != nullptr; p = std::strstr(p, key)) {
    std::memmove(p, p + kl, std::strlen(p + kl) + 1);
  }
}

// mbedTLS error class check, matching backend_client.cpp: certificate/PEM problems vs. everything else.
bool is_cert_error(int err) {
  if (err >= 0) return false;
  const int high = (-err) & 0x7F80;
  if (high >= 0x2080 && high <= 0x3000) return true;  // X.509 (verify failed, bad certificate, ...)
  if (high >= 0x1080 && high <= 0x1480) return true;  // PEM: the CA bundle could not be parsed
  return high == ((-MBEDTLS_ERR_SSL_CA_CHAIN_REQUIRED) & 0x7F80) ||
         high == ((-MBEDTLS_ERR_SSL_PEER_VERIFY_FAILED) & 0x7F80) ||
         high == ((-MBEDTLS_ERR_SSL_BAD_HS_CERTIFICATE) & 0x7F80);
}

// Bounded reader over the TLS socket with an absolute millisecond deadline.
class Rx {
 public:
  Rx(WiFiClientSecure& c, uint32_t deadline_ms) : c_(c), deadline_(deadline_ms) {}

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
          return buf_[0];
        }
      }
      if (a < 0 || !c_.connected()) return -1;
      if (static_cast<int32_t>(millis() - deadline_) >= 0) {
        timed_out_ = true;
        return -1;
      }
      vTaskDelay(pdMS_TO_TICKS(5));
    }
  }

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

  bool timed_out() const { return timed_out_; }

 private:
  WiFiClientSecure& c_;
  uint32_t deadline_;
  uint8_t buf_[256];
  size_t pos_ = 0;
  size_t len_ = 0;
  bool timed_out_ = false;
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

// Reads the response into s_resp. Returns the HTTP status (>=100) on success, or a negative marker:
// -1 timed out, -2 transport lost, -3 malformed, -4 body over the cap.
int read_response(Rx& rx, size_t& resp_len) {
  resp_len = 0;
  s_resp[0] = '\0';
  char line[192];
  size_t len = 0;
  if (!rx.line(line, sizeof(line), len)) return rx.timed_out() ? -1 : -2;
  int status = 0;
  if (std::strncmp(line, "HTTP/1.", 7) != 0 || len < 12 || sscanf(line + 9, "%3d", &status) != 1 || status < 100) {
    return -3;
  }
  bool chunked = false;
  long content_len = -1;
  for (;;) {
    if (!rx.line(line, sizeof(line), len)) return rx.timed_out() ? -1 : -2;
    if (len == 0) break;
    const char* v = nullptr;
    if (header_is(line, "content-length", v)) {
      content_len = strtol(v, nullptr, 10);
    } else if (header_is(line, "transfer-encoding", v)) {
      chunked = contains_ci(v, "chunked");
    }
  }

  bool too_large = false;
  auto store = [&](int c) {
    if (resp_len < kRespCap) {
      s_resp[resp_len++] = static_cast<char>(c);
    } else {
      too_large = true;
    }
  };
  if (status == 204 || status == 304 || status < 200) {
    // no body
  } else if (chunked) {
    for (;;) {
      if (!rx.line(line, sizeof(line), len)) return rx.timed_out() ? -1 : -2;
      const unsigned long sz = strtoul(line, nullptr, 16);
      if (sz == 0) {
        while (rx.line(line, sizeof(line), len) && len > 0) {
        }
        break;
      }
      for (unsigned long i = 0; i < sz; ++i) {
        const int c = rx.get();
        if (c < 0) return rx.timed_out() ? -1 : -2;
        store(c);
      }
      if (!rx.line(line, sizeof(line), len)) return rx.timed_out() ? -1 : -2;  // trailing CRLF
    }
  } else if (content_len >= 0) {
    for (long i = 0; i < content_len; ++i) {
      const int c = rx.get();
      if (c < 0) return rx.timed_out() ? -1 : -2;
      store(c);
    }
  } else {
    for (;;) {
      const int c = rx.get();
      if (c < 0) break;  // server closed: end of body
      store(c);
    }
  }
  s_resp[resp_len] = '\0';
  if (too_large) {
    resp_len = 0;
    s_resp[0] = '\0';
    return -4;
  }
  return status;
}

}  // namespace

const char* to_string(Status s) {
  switch (s) {
    case Status::Ok: return "ok";
    case Status::NotConfigured: return "not_configured";
    case Status::NoLink: return "no_link";
    case Status::TlsError: return "tls_error";
    case Status::HttpError: return "http_error";
    case Status::Timeout: return "timeout";
    case Status::BadResponse: return "bad_response";
    case Status::TooLarge: return "too_large";
  }
  return "bad_response";
}

const char* default_ca_pem() { return kGroqRootCa; }

Status ask(const Config& cfg, const char* question, char* answer_out, size_t answer_cap, char* err_out,
           size_t err_cap) {
  if (answer_out != nullptr && answer_cap > 0) answer_out[0] = '\0';
  if (err_out != nullptr && err_cap > 0) err_out[0] = '\0';

  if (cfg.host == nullptr || cfg.host[0] == '\0' || cfg.port == 0 || cfg.path == nullptr || cfg.path[0] == '\0' ||
      cfg.model == nullptr || cfg.model[0] == '\0' || cfg.api_key == nullptr || cfg.api_key[0] == '\0' ||
      cfg.ca_pem == nullptr || cfg.ca_pem[0] == '\0') {
    set_err(err_out, err_cap, "direct AI is not configured");
    return Status::NotConfigured;
  }
  if (question == nullptr || question[0] == '\0') {
    set_err(err_out, err_cap, "no question to ask");
    return Status::BadResponse;
  }
  if (WiFi.status() != WL_CONNECTED) {
    set_err(err_out, err_cap, "Wi-Fi not connected");
    return Status::NoLink;
  }

  const size_t body_len = build_chat_body(cfg.model, question, s_body, sizeof(s_body));
  if (body_len == 0) {
    set_err(err_out, err_cap, "question too long to send");
    return Status::TooLarge;
  }

  const uint32_t budget = cfg.timeout_ms != 0 ? cfg.timeout_ms : kDefaultTimeoutMs;
  const uint32_t start = millis();
  const uint32_t deadline = start + budget;

  WiFiClientSecure* cli = new (std::nothrow) WiFiClientSecure();
  if (cli == nullptr) {
    set_err(err_out, err_cap, "out of memory");
    return Status::BadResponse;
  }
  cli->setCACert(cfg.ca_pem);  // validated TLS only; never setInsecure
  cli->setHandshakeTimeout(cfg::kTlsHandshakeTimeoutS);
  cli->setTimeout(budget / 1000 + 1);  // socket send/recv timeout in seconds

  Status result = Status::BadResponse;
  do {
    const int ok = cli->connect(cfg.host, cfg.port, static_cast<int32_t>(budget));
    if (ok != 1) {
      char text[80] = "";
      const int err = cli->lastError(text, sizeof(text));
      if (WiFi.status() != WL_CONNECTED) {
        set_err(err_out, err_cap, "Wi-Fi lost");
        result = Status::NoLink;
      } else if (is_cert_error(err)) {
        set_err(err_out, err_cap, "server certificate rejected (check the CA bundle)");
        result = Status::TlsError;
      } else {
        set_err(err_out, err_cap, "could not establish a secure connection");
        result = Status::TlsError;
      }
      break;
    }

    // Request head carries the bearer token; build it, send it, then wipe it immediately.
    char host_hdr[128];
    if (cfg.port == 443) {
      snprintf(host_hdr, sizeof(host_hdr), "%s", cfg.host);
    } else {
      snprintf(host_hdr, sizeof(host_hdr), "%s:%u", cfg.host, static_cast<unsigned>(cfg.port));
    }
    char head[640];
    const int n = snprintf(head, sizeof(head),
                           "POST %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: oyster-pocket/%s\r\n"
                           "Accept: application/json\r\nAuthorization: Bearer %s\r\n"
                           "Content-Type: application/json\r\nContent-Length: %u\r\nConnection: close\r\n\r\n",
                           cfg.path, host_hdr, OYSTER_FIRMWARE_VERSION, cfg.api_key,
                           static_cast<unsigned>(body_len));
    if (n <= 0 || static_cast<size_t>(n) >= sizeof(head)) {
      secure_zero(head, sizeof(head));
      set_err(err_out, err_cap, "request could not be built");
      result = Status::BadResponse;
      break;
    }
    const size_t w = cli->write(reinterpret_cast<const uint8_t*>(head), static_cast<size_t>(n));
    secure_zero(head, sizeof(head));  // carried the bearer token
    if (w != static_cast<size_t>(n) ||
        cli->write(reinterpret_cast<const uint8_t*>(s_body), body_len) != body_len) {
      set_err(err_out, err_cap, "connection lost while sending the request");
      result = Status::TlsError;
      break;
    }

    Rx rx(*cli, deadline);
    size_t resp_len = 0;
    const int status = read_response(rx, resp_len);
    if (status == -1) {
      set_err(err_out, err_cap, "request timed out");
      result = Status::Timeout;
      break;
    }
    if (status == -4) {
      set_err(err_out, err_cap, "reply larger than %u bytes", static_cast<unsigned>(kRespCap));
      result = Status::TooLarge;
      break;
    }
    if (status < 0) {
      set_err(err_out, err_cap, "incomplete or malformed reply");
      result = Status::BadResponse;
      break;
    }
    if (status != 200) {
      char msg[160];
      parse_chat_answer(s_resp, msg, sizeof(msg));  // pulls error.message when present (false either way)
      scrub_key(msg, cfg.api_key);
      if (msg[0] != '\0') {
        set_err(err_out, err_cap, "server error (HTTP %d): %s", status, msg);
      } else {
        set_err(err_out, err_cap, "server error (HTTP %d)", status);
      }
      scrub_key(err_out, cfg.api_key);
      result = Status::HttpError;
      break;
    }
    if (parse_chat_answer(s_resp, answer_out, answer_cap)) {
      result = Status::Ok;
    } else {
      if (answer_out != nullptr && answer_cap > 0) answer_out[0] = '\0';
      set_err(err_out, err_cap, "the reply was not a usable answer");
      result = Status::BadResponse;
    }
  } while (false);

  cli->stop();
  delete cli;
  secure_zero(s_body, sizeof(s_body));  // held the question, not the key, but clear it anyway
  return result;
}

}  // namespace oyster::directai
