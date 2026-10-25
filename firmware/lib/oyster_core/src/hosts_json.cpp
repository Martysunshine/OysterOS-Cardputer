// Parsers and screen text for the Hosts module. See oyster_core/hosts_json.h.
#include "oyster_core/hosts_json.h"

#include <ArduinoJson.h>

#include <cmath>
#include <cstdio>
#include <cstring>

namespace oyster::hosts {
namespace {

constexpr uint8_t kNesting = 6;  // replies are objects of depth 4 at most

bool id_char(char c) {
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_' || c == '-';
}

// ---- JSON helpers ------------------------------------------------------------------------------------------

Parse load(JsonDocument& doc, const JsonDocument& filter, const char* json, size_t len) {
  if (json == nullptr || len == 0) return Parse::Malformed;
  if (len > kMaxReply) return Parse::Oversized;
  const DeserializationError err = deserializeJson(doc, json, len, DeserializationOption::Filter(filter),
                                                   DeserializationOption::NestingLimit(kNesting));
  return err ? Parse::Malformed : Parse::Ok;
}

bool read_percent(JsonVariantConst v, uint8_t& out) {
  if (!v.is<float>()) return false;  // integers and decimals; never strings or booleans
  const float f = v.as<float>();
  if (!std::isfinite(f) || f < 0.0f || f > 100.0f) return false;
  out = percent_of(f);
  return true;
}

bool read_load(JsonVariantConst v, uint32_t& out) {
  if (!v.is<float>()) return false;
  const float f = v.as<float>();
  if (!std::isfinite(f) || f < 0.0f || f > 10000.0f) return false;
  out = static_cast<uint32_t>(f * 100.0f + 0.5f);
  return true;
}

bool read_uptime(JsonVariantConst v, uint32_t& out) {
  if (!v.is<uint32_t>()) return false;  // whole non-negative seconds only
  const uint32_t s = v.as<uint32_t>();
  if (s > 315360000u) return false;  // ten years, the backend's bound
  out = s;
  return true;
}

bool metric_by_name(const char* name, Metric& m) {
  static const struct {
    const char* name;
    Metric metric;
  } kNames[] = {{"cpu_percent", kCpu},         {"load_1m", kLoad},      {"mem_used_percent", kMem},
                {"disk_used_percent", kDisk}, {"uptime_s", kUptime},   {"services", kServices}};
  for (const auto& e : kNames) {
    if (std::strcmp(name, e.name) == 0) {
      m = e.metric;
      return true;
    }
  }
  return false;
}

ServiceState service_state(const char* s) {
  if (s == nullptr) return ServiceState::Unknown;
  if (std::strcmp(s, "active") == 0) return ServiceState::Active;
  if (std::strcmp(s, "inactive") == 0) return ServiceState::Inactive;
  if (std::strcmp(s, "failed") == 0) return ServiceState::Failed;
  return ServiceState::Unknown;
}

// ---- bounded text builder ----------------------------------------------------------------------------------

class Out {
 public:
  Out(char* b, size_t n) : b_(b), n_(n) {
    if (n_ > 0) b_[0] = '\0';
  }
  void put(const char* s) {
    while (n_ > 0 && *s != '\0' && len_ + 1 < n_) b_[len_++] = *s++;
    if (n_ > 0) b_[len_] = '\0';
  }
  void line(const char* s) {
    if (len_ > 0) put("\n");
    put(s);
  }

 private:
  char* b_;
  size_t n_;
  size_t len_ = 0;
};

const char* metric_label(Metric m) {
  switch (m) {
    case kCpu: return "CPU";
    case kLoad: return "LOAD";
    case kMem: return "MEM";
    case kDisk: return "DISK";
    case kUptime: return "UP";
    case kServices: return "SVC";
  }
  return "?";
}

// "CPU 12%", "CPU --" (allowlisted but not delivered) or nothing (the host does not share it).
bool metric_item(const Health& h, Metric m, char* out, size_t n) {
  if ((h.have & metric_bit(m)) != 0) {
    char v[24];
    switch (m) {
      case kCpu: std::snprintf(v, sizeof(v), "%u%%", static_cast<unsigned>(h.cpu)); break;
      case kMem: std::snprintf(v, sizeof(v), "%u%%", static_cast<unsigned>(h.mem)); break;
      case kDisk: std::snprintf(v, sizeof(v), "%u%%", static_cast<unsigned>(h.disk)); break;
      case kLoad: format_load(h.load_x100, v, sizeof(v)); break;
      case kUptime: format_uptime(h.uptime_s, v, sizeof(v)); break;
      case kServices: return false;
    }
    std::snprintf(out, n, "%s %s", metric_label(m), v);
    return true;
  }
  if ((h.missing & metric_bit(m)) != 0 && m != kServices) {
    std::snprintf(out, n, "%s --", metric_label(m));
    return true;
  }
  return false;
}

void metric_line(const Health& h, Metric a, Metric b, Out& o) {
  char x[32], y[32], line[72];
  const bool ha = metric_item(h, a, x, sizeof(x));
  const bool hb = b != a && metric_item(h, b, y, sizeof(y));
  if (ha && hb) {
    std::snprintf(line, sizeof(line), "%s  %s", x, y);
  } else if (ha) {
    std::snprintf(line, sizeof(line), "%s", x);
  } else if (hb) {
    std::snprintf(line, sizeof(line), "%s", y);
  } else {
    return;
  }
  o.line(line);
}

// One list row: `name` on the left, `state` right-aligned, `cols` characters in all.
void format_row(char* out, size_t n, size_t cols, const char* name, const char* state) {
  if (n == 0) return;
  if (cols >= n) cols = n - 1;
  const size_t sl = std::strlen(state);
  const size_t nl = std::strlen(name);
  const size_t room = (sl > 0 && cols > sl + 1) ? cols - sl - 1 : cols;
  const bool with_state = sl > 0 && cols > sl + 1;
  size_t o = 0;
  if (nl > room) {
    for (; o + 1 < room; ++o) out[o] = name[o];
    if (room > 0) out[o++] = '~';
  } else {
    for (; o < nl; ++o) out[o] = name[o];
  }
  if (with_state) {
    while (o < room) out[o++] = ' ';
    out[o++] = ' ';
    for (size_t i = 0; i < sl; ++i) out[o++] = state[i];
  }
  out[o] = '\0';
}

}  // namespace

// ---- text helpers ------------------------------------------------------------------------------------------

bool valid_id(const char* id) {
  if (id == nullptr) return false;
  size_t n = 0;
  for (; id[n] != '\0'; ++n) {
    if (n >= kIdLen || !id_char(id[n])) return false;
  }
  return n > 0;
}

bool health_path(const char* id, char* out, size_t n) {
  if (out == nullptr || n == 0) return false;
  out[0] = '\0';
  if (!valid_id(id)) return false;
  const int w = std::snprintf(out, n, "/v1/hosts/%s/health", id);
  if (w < 0 || static_cast<size_t>(w) >= n) {
    out[0] = '\0';
    return false;
  }
  return true;
}

void sanitize_text(char* dst, size_t cap, const char* src) {
  if (dst == nullptr || cap == 0) return;
  size_t o = 0;
  if (src != nullptr) {
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(src); *p != 0 && o + 1 < cap; ++p) {
      char c;
      if (*p >= 0xC0) {
        c = '?';  // lead byte of a multi-byte character
      } else if (*p >= 0x80) {
        continue;  // continuation byte
      } else if (*p < 0x20 || *p == 0x7F) {
        c = '?';
      } else {
        c = static_cast<char>(*p);
      }
      if (c == ' ' && o == 0) continue;  // trim leading spaces
      dst[o++] = c;
    }
  }
  while (o > 0 && dst[o - 1] == ' ') --o;  // trim trailing spaces
  dst[o] = '\0';
}

uint8_t percent_of(float v) {
  if (!(v >= 0.0f)) return 0;  // also NaN
  if (v >= 99.5f) return 100;
  return static_cast<uint8_t>(v + 0.5f);
}

void format_uptime(uint32_t s, char* out, size_t n) {
  if (s < 60u) {
    std::snprintf(out, n, "%us", static_cast<unsigned>(s));
  } else if (s < 3600u) {
    std::snprintf(out, n, "%um", static_cast<unsigned>(s / 60u));
  } else if (s < 86400u) {
    std::snprintf(out, n, "%uh %um", static_cast<unsigned>(s / 3600u), static_cast<unsigned>((s % 3600u) / 60u));
  } else {
    std::snprintf(out, n, "%ud %uh", static_cast<unsigned>(s / 86400u), static_cast<unsigned>((s % 86400u) / 3600u));
  }
}

void format_age(uint32_t s, char* out, size_t n) {
  if (s < 60u) {
    std::snprintf(out, n, "%u s", static_cast<unsigned>(s));
  } else if (s < 3600u) {
    std::snprintf(out, n, "%u min", static_cast<unsigned>(s / 60u));
  } else if (s < 86400u) {
    std::snprintf(out, n, "%u h", static_cast<unsigned>(s / 3600u));
  } else {
    std::snprintf(out, n, ">1 d");
  }
}

void format_load(uint32_t x100, char* out, size_t n) {
  std::snprintf(out, n, "%u.%02u", static_cast<unsigned>(x100 / 100u), static_cast<unsigned>(x100 % 100u));
}

const char* service_state_text(ServiceState s) {
  switch (s) {
    case ServiceState::Active: return "active";
    case ServiceState::Inactive: return "inactive";
    case ServiceState::Failed: return "failed";
    case ServiceState::Unknown: break;
  }
  return "unknown";
}

const char* host_state_text(HostState s) {
  switch (s) {
    case HostState::Ok: return "ok";
    case HostState::Partial: return "partial";
    case HostState::Down: return "down";
    case HostState::Unknown: break;
  }
  return "";
}

void format_host_row(const Host& h, char* out, size_t n, size_t cols) {
  format_row(out, n, cols, h.name, host_state_text(h.state));
}

void format_service_row(const Service& s, char* out, size_t n, size_t cols) {
  format_row(out, n, cols, s.name, service_state_text(s.state));
}

void format_host_count(uint8_t count, bool more, char* out, size_t n) {
  if (count == 0) {
    std::snprintf(out, n, "no hosts");
  } else if (count == 1 && !more) {
    std::snprintf(out, n, "1 host");
  } else {
    std::snprintf(out, n, "%u%s hosts", static_cast<unsigned>(count), more ? "+" : "");
  }
}

void format_overview(const Health& h, uint32_t age_s, char* out, size_t n) {
  Out o(out, n);
  metric_line(h, kCpu, kMem, o);
  metric_line(h, kDisk, kUptime, o);
  metric_line(h, kLoad, kLoad, o);

  char age[16], line[64];
  format_age(age_s, age, sizeof(age));
  std::snprintf(line, sizeof(line), "State %s, %s ago", h.partial ? "partial" : "ok", age);
  o.line(line);

  if (h.missing != 0) {
    char names[48];
    Out nm(names, sizeof(names));
    nm.put("No data:");
    for (uint8_t m = 0; m <= kServices; ++m) {
      if ((h.missing & metric_bit(static_cast<Metric>(m))) == 0) continue;
      nm.put(" ");
      nm.put(metric_label(static_cast<Metric>(m)));
    }
    o.line(names);
  }
  if ((h.have & metric_bit(kServices)) != 0) {
    if (h.service_count > 0) {
      std::snprintf(line, sizeof(line), "Enter: services (%u)", static_cast<unsigned>(h.service_count));
      o.line(line);
    } else {
      o.line("No services listed");
    }
  }
}

// ---- host list ---------------------------------------------------------------------------------------------

int find_host(const HostList& list, const char* id) {
  if (id == nullptr) return -1;
  for (uint8_t i = 0; i < list.count && i < kMaxHosts; ++i) {
    if (std::strcmp(list.hosts[i].id, id) == 0) return i;
  }
  return -1;
}

void carry_states(const HostList& previous, HostList& next) {
  for (uint8_t i = 0; i < next.count && i < kMaxHosts; ++i) {
    const int j = find_host(previous, next.hosts[i].id);
    if (j >= 0) next.hosts[i].state = previous.hosts[j].state;
  }
}

Parse parse_host_list(const char* json, size_t len, HostList& out) {
  out = HostList{};
  JsonDocument filter;
  filter["hosts"][0]["host_id"] = true;
  filter["hosts"][0]["display_name"] = true;
  JsonDocument doc;
  const Parse loaded = load(doc, filter, json, len);
  if (loaded != Parse::Ok) return loaded;

  JsonArrayConst hosts = doc["hosts"].as<JsonArrayConst>();
  if (hosts.isNull()) return Parse::Malformed;
  for (JsonVariantConst item : hosts) {
    JsonObjectConst obj = item.as<JsonObjectConst>();
    const JsonString id_json = obj["host_id"].as<JsonString>();
    const char* id = id_json.c_str();
    // an id with an embedded NUL would be read back shorter than it was sent: refuse it
    if (obj.isNull() || id == nullptr || id_json.size() != std::strlen(id) || !valid_id(id) ||
        find_host(out, id) >= 0) {
      if (out.skipped < 255) ++out.skipped;
      continue;
    }
    if (out.count >= kMaxHosts) {
      out.more = true;
      continue;
    }
    Host& h = out.hosts[out.count++];
    std::memcpy(h.id, id, std::strlen(id) + 1);  // valid_id() bounded it to kIdLen
    sanitize_text(h.name, sizeof(h.name), obj["display_name"].as<const char*>());
    if (h.name[0] == '\0') std::memcpy(h.name, h.id, std::strlen(h.id) + 1);
    h.state = HostState::Unknown;
  }
  return Parse::Ok;
}

// ---- health report -----------------------------------------------------------------------------------------

Parse parse_health(const char* json, size_t len, const char* expect_id, Health& out) {
  out = Health{};
  JsonDocument filter;
  filter["host_id"] = true;
  filter["state"] = true;
  filter["metrics"]["cpu_percent"] = true;
  filter["metrics"]["load_1m"] = true;
  filter["metrics"]["mem_used_percent"] = true;
  filter["metrics"]["disk_used_percent"] = true;
  filter["metrics"]["uptime_s"] = true;
  filter["services"][0]["name"] = true;
  filter["services"][0]["state"] = true;
  filter["missing"] = true;
  JsonDocument doc;
  const Parse loaded = load(doc, filter, json, len);
  if (loaded != Parse::Ok) return loaded;
  if (!doc.is<JsonObject>()) return Parse::Malformed;

  const char* id = doc["host_id"].as<const char*>();
  if (id == nullptr || (expect_id != nullptr && std::strcmp(id, expect_id) != 0)) return Parse::Invalid;
  const char* state = doc["state"].as<const char*>();
  if (state == nullptr) return Parse::Invalid;
  bool partial;
  if (std::strcmp(state, "ok") == 0) {
    partial = false;
  } else if (std::strcmp(state, "partial") == 0) {
    partial = true;
  } else {
    return Parse::Invalid;
  }

  // What the host says it could not deliver (unknown names are ignored).
  for (JsonVariantConst name : doc["missing"].as<JsonArrayConst>()) {
    Metric m;
    const char* s = name.as<const char*>();
    if (s != nullptr && metric_by_name(s, m)) out.missing |= metric_bit(m);
  }

  JsonObjectConst metrics = doc["metrics"].as<JsonObjectConst>();
  // A value that is present but unusable (wrong type, out of range) counts as missing, as in the backend.
  auto number = [&](const char* key, Metric m, bool ok) {
    if (ok) {
      out.have |= metric_bit(m);
      out.missing &= static_cast<uint8_t>(~metric_bit(m));
    } else if (!metrics[key].isNull()) {
      out.missing |= metric_bit(m);
    }
  };
  number("cpu_percent", kCpu, read_percent(metrics["cpu_percent"], out.cpu));
  number("mem_used_percent", kMem, read_percent(metrics["mem_used_percent"], out.mem));
  number("disk_used_percent", kDisk, read_percent(metrics["disk_used_percent"], out.disk));
  number("load_1m", kLoad, read_load(metrics["load_1m"], out.load_x100));
  number("uptime_s", kUptime, read_uptime(metrics["uptime_s"], out.uptime_s));

  JsonArrayConst services = doc["services"].as<JsonArrayConst>();
  if (!services.isNull()) {
    out.have |= metric_bit(kServices);
    out.missing &= static_cast<uint8_t>(~metric_bit(kServices));
    for (JsonVariantConst item : services) {
      if (out.service_count >= kMaxServices) break;
      JsonObjectConst obj = item.as<JsonObjectConst>();
      Service& sv = out.services[out.service_count];
      sanitize_text(sv.name, sizeof(sv.name), obj["name"].as<const char*>());
      if (obj.isNull() || sv.name[0] == '\0') continue;
      sv.state = service_state(obj["state"].as<const char*>());
      ++out.service_count;
    }
  }

  out.partial = partial || out.missing != 0;
  return Parse::Ok;
}

// ---- failures ----------------------------------------------------------------------------------------------

namespace {

// 502 body of the backend: {"error":"host_unavailable","state":"unreachable"|"timeout",...}
Fail unavailable_fail(const char* body, size_t len) {
  if (body == nullptr || len == 0 || len > kMaxReply) return Fail::Server;
  JsonDocument filter;
  filter["error"] = true;
  filter["state"] = true;
  JsonDocument doc;
  if (deserializeJson(doc, body, len, DeserializationOption::Filter(filter),
                      DeserializationOption::NestingLimit(kNesting))) {
    return Fail::Server;
  }
  const char* error = doc["error"].as<const char*>();
  const char* state = doc["state"].as<const char*>();
  if (error == nullptr || std::strcmp(error, "host_unavailable") != 0 || state == nullptr) return Fail::Server;
  if (std::strcmp(state, "timeout") == 0) return Fail::Timeout;
  if (std::strcmp(state, "unreachable") == 0) return Fail::Unreachable;
  return Fail::Server;
}

}  // namespace

Fail classify_reply(int status, Ctx ctx, const char* body, size_t len) {
  (void)ctx;
  if (status == 200) return Fail::None;
  if (status < 0) {
    switch (status) {
      case -6: return Fail::Clock;
      case -5: return Fail::TooLarge;
      case -4:
      case -3: return Fail::Transport;
      default: return Fail::Offline;  // -1 and anything unknown
    }
  }
  if (status == 401 || status == 403) return Fail::Auth;
  if (status == 404) return Fail::NotFound;
  if (status == 429) return Fail::Busy;
  if (status == 502) return unavailable_fail(body, len);
  if (status >= 500) return Fail::Server;
  if (status >= 400) return Fail::Refused;
  return Fail::Malformed;  // 1xx, 3xx and 2xx other than 200 are not part of the contract
}

Fail from_parse(Parse p) {
  switch (p) {
    case Parse::Ok: return Fail::None;
    case Parse::Oversized: return Fail::TooLarge;
    case Parse::Malformed:
    case Parse::Invalid: break;
  }
  return Fail::Malformed;
}

const char* fail_label(Fail f) {
  switch (f) {
    case Fail::None: return "";
    case Fail::Offline: return "offline";
    case Fail::Clock: return "clock not set";
    case Fail::Transport: return "connection lost";
    case Fail::TooLarge: return "reply too large";
    case Fail::Auth: return "not authorised";
    case Fail::NotFound: return "not found";
    case Fail::Unreachable: return "host unreachable";
    case Fail::Timeout: return "host timed out";
    case Fail::Busy: return "busy";
    case Fail::Server: return "backend error";
    case Fail::Refused: return "refused";
    case Fail::Malformed: return "bad reply";
    case Fail::NoAnswer: return "no answer";
    case Fail::Cancelled: return "cancelled";
  }
  return "error";
}

void fail_text(Fail f, Ctx ctx, int status, const char* detail, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  char http[24] = "";
  if (status > 0) std::snprintf(http, sizeof(http), " (HTTP %d)", status);
  char msg[200];
  bool with_detail = false;
  switch (f) {
    case Fail::None: msg[0] = '\0'; break;
    case Fail::Offline:
      std::snprintf(msg, sizeof(msg), "Backend offline or not set up.\nCheck Wi-Fi and the backend settings.");
      with_detail = true;
      break;
    case Fail::Clock:
      std::snprintf(msg, sizeof(msg), "Device clock not set; nothing was sent.\nWait for time sync.");
      break;
    case Fail::Transport:
      std::snprintf(msg, sizeof(msg), "The connection to the backend broke.");
      with_detail = true;
      break;
    case Fail::TooLarge:
      std::snprintf(msg, sizeof(msg), "The backend reply is too large for this device.");
      break;
    case Fail::Auth:
      std::snprintf(msg, sizeof(msg), "The backend rejected this device%s.\nRe-pair or re-provision it.", http);
      break;
    case Fail::NotFound:
      if (ctx == Ctx::List) {
        std::snprintf(msg, sizeof(msg), "The backend has no host API%s.\nUpdate the backend.", http);
      } else {
        std::snprintf(msg, sizeof(msg), "Host not found, or no longer shared with this device.\nDel, then r: new list.");
      }
      break;
    case Fail::Unreachable: std::snprintf(msg, sizeof(msg), "The backend could not reach the host."); break;
    case Fail::Timeout: std::snprintf(msg, sizeof(msg), "The host did not answer in time."); break;
    case Fail::Busy: std::snprintf(msg, sizeof(msg), "The backend or the device is busy.\nWait a moment."); break;
    case Fail::Server: std::snprintf(msg, sizeof(msg), "Backend error%s.", http); break;
    case Fail::Refused: std::snprintf(msg, sizeof(msg), "The backend refused the request%s.", http); break;
    case Fail::Malformed: std::snprintf(msg, sizeof(msg), "Unexpected reply from the backend."); break;
    case Fail::NoAnswer:
      std::snprintf(msg, sizeof(msg), "No answer from the backend in time.\nThe radio may be busy.");
      break;
    case Fail::Cancelled: std::snprintf(msg, sizeof(msg), "Cancelled."); break;
  }
  Out o(out, n);
  o.put(msg);
  if (with_detail && detail != nullptr && detail[0] != '\0') {
    char d[96];
    sanitize_text(d, sizeof(d), detail);
    if (d[0] != '\0') {
      o.put("\n\n");
      o.put(d);
    }
  }
  // A host that is gone cannot be retried; the list has to be refreshed first.
  if (f != Fail::None && !(f == Fail::NotFound && ctx == Ctx::Health)) o.put("\n\nEnter or r: retry");
}

}  // namespace oyster::hosts
