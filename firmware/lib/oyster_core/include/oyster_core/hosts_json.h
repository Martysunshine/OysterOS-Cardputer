// Read-only host health for the Hosts module (src/modules/hosts.cpp): bounded parsers for the two backend
// replies (GET /v1/hosts, GET /v1/hosts/{id}/health, docs/COMPANION_API.md), the text the screens show and
// the classification of failures. Pure logic, no Arduino headers; host-tested in test/test_hosts.
//
// The backend is trusted to be well-formed but never to be small or polite: every parser checks the input
// size, caps nesting, keeps only the fields it needs, validates every identifier that is later used in a
// request path, and writes into fixed structs (no allocation outside the JSON parser, no overrun).
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::hosts {

constexpr size_t kMaxHosts = 8;      // the backend lists up to 16; the device keeps the first 8
constexpr size_t kMaxServices = 16;  // same cap as the backend
constexpr size_t kIdLen = 40;        // host ids are [0-9A-Za-z_-]{1,40}
constexpr size_t kNameLen = 40;      // display text kept per host and per service (longer text is cut)
constexpr size_t kMaxReply = 4095;   // replies of 4096 bytes or more are refused (contract: < 4 KiB)

enum class Parse : uint8_t {
  Ok,
  Oversized,  // reply at or above 4 KiB (or cut by the client buffer)
  Malformed,  // not JSON, too deeply nested, or not the expected shape
  Invalid,    // well-formed but wrong content: another host's report, unknown state
};

// ---- host list ---------------------------------------------------------------------------------------------

enum class HostState : uint8_t { Unknown, Ok, Partial, Down };  // Unknown until the host's health was read

struct Host {
  char id[kIdLen + 1];
  char name[kNameLen + 1];
  HostState state;
};

struct HostList {
  Host hosts[kMaxHosts];
  uint8_t count;
  uint8_t skipped;  // entries dropped: bad id, duplicate id, not an object
  bool more;        // the backend listed more valid hosts than kMaxHosts
};

Parse parse_host_list(const char* json, size_t len, HostList& out);

// Keeps the states already learned for hosts that are still listed (matched by id) after a refresh.
void carry_states(const HostList& previous, HostList& next);
// Index of the host with this id, or -1.
int find_host(const HostList& list, const char* id);

// ---- health report -----------------------------------------------------------------------------------------

// Bit positions of the report's metrics (Health::have / Health::missing).
enum Metric : uint8_t { kCpu = 0, kLoad = 1, kMem = 2, kDisk = 3, kUptime = 4, kServices = 5 };
constexpr uint8_t metric_bit(Metric m) { return static_cast<uint8_t>(1u << m); }

enum class ServiceState : uint8_t { Active, Inactive, Failed, Unknown };

struct Service {
  char name[kNameLen + 1];
  ServiceState state;
};

struct Health {
  bool partial;           // the backend said "partial", or a value had to be dropped
  uint8_t have;           // metric_bit(Metric): the value is present and in range (kServices: the list is present)
  uint8_t missing;        // metric_bit(Metric): allowlisted by the host but not delivered
  uint8_t cpu;            // percent, 0..100
  uint8_t mem;
  uint8_t disk;
  uint32_t load_x100;     // 1-minute load average times 100
  uint32_t uptime_s;
  uint8_t service_count;  // entries of `services` in use
  Service services[kMaxServices];
};

// `expect_id` (when not null) must equal the report's host_id, so a reply for another host is never shown.
Parse parse_health(const char* json, size_t len, const char* expect_id, Health& out);

// ---- failures ----------------------------------------------------------------------------------------------

enum class Fail : uint8_t {
  None,
  Offline,      // no link, not provisioned, in back-off (client status -1) or any unknown transport failure
  Clock,        // device clock not set, nothing was sent (-6)
  Transport,    // connection broke while talking (-3, -4)
  TooLarge,     // reply larger than the contract allows (-5, or 4 KiB and more)
  Auth,         // 401, 403
  NotFound,     // 404
  Unreachable,  // 502 host_unavailable / unreachable (or 502 without a readable body)
  Timeout,      // 502 host_unavailable / timeout
  Busy,         // 429, or the request queue was full
  Server,       // other 5xx
  Refused,      // other 4xx
  Malformed,    // 200 with a body that could not be used
  NoAnswer,     // nothing came back in time (radio busy, request lost)
  Cancelled,    // the user cancelled
};

enum class Ctx : uint8_t { List, Health };

// Maps the client's return value (HTTP status, or a negative transport code) and the reply body to a failure.
// 200 maps to None; the caller then parses the body. A 502 body is read for its "state".
Fail classify_reply(int status, Ctx ctx, const char* body, size_t len);
// Fail for a body that arrived with 200 but did not parse.
Fail from_parse(Parse p);

// Short label for an alert heading ("offline", "unreachable"), at most 18 characters.
const char* fail_label(Fail f);
// Full explanation for a text page, ending with the retry hint. `detail` (optional) is the client's last
// error text; it is appended for transport-level failures only.
void fail_text(Fail f, Ctx ctx, int status, const char* detail, char* out, size_t n);

// ---- text for the screens ----------------------------------------------------------------------------------

bool valid_id(const char* id);                                // [0-9A-Za-z_-]{1,40}
bool health_path(const char* id, char* out, size_t n);       // "/v1/hosts/<id>/health", false if id invalid
// Printable ASCII copy: control characters become '?', a multi-byte UTF-8 character becomes one '?',
// surrounding spaces are trimmed, the result is cut to cap-1 characters.
void sanitize_text(char* dst, size_t cap, const char* src);

uint8_t percent_of(float v);                                  // rounded and clamped to 0..100
void format_uptime(uint32_t seconds, char* out, size_t n);    // "45s", "12m", "5h 12m", "2d 3h"
void format_age(uint32_t seconds, char* out, size_t n);       // "12 s", "3 min", "2 h", ">1 d"
void format_load(uint32_t x100, char* out, size_t n);         // "0.42"
const char* service_state_text(ServiceState s);               // "active", "inactive", "failed", "unknown"
const char* host_state_text(HostState s);                     // "", "ok", "partial", "down"

// One list row of `cols` characters: the name on the left, the state right-aligned. A name that does not
// fit ends in '~'.
void format_host_row(const Host& h, char* out, size_t n, size_t cols);
void format_service_row(const Service& s, char* out, size_t n, size_t cols);

// The overview page: metric lines, state with the age of the reading, what is missing, the services hint.
void format_overview(const Health& h, uint32_t age_s, char* out, size_t n);

// Short Home-tile text (at most 11 characters): "3 hosts", "1 host", "no hosts".
void format_host_count(uint8_t count, bool more, char* out, size_t n);

}  // namespace oyster::hosts
