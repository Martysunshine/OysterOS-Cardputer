// Hosts module (Home tile 3, "Linux"): read-only health of the hosts the backend shares with this device.
// Screens: host list (L10) > overview (L11) > services (L12) and an honest "no log source" page (L13).
// Nothing is executed on any host and nothing is sent to one: the device only reads two backend endpoints,
// GET /v1/hosts and GET /v1/hosts/{id}/health (docs/COMPANION_API.md). Parsing and screen text live in
// oyster_core/hosts_json.h (host-tested); this file holds the page state and the hand-off to the worker task.
//
// Threads: key handlers, view() and tick() run on the UI task; call() runs on the worker task. The shared
// state is copied under modules::Guard, which is never held across a request. Every request carries a token;
// a reply whose token is no longer the latest (cancelled, timed out, superseded) is dropped.
#include <Arduino.h>

#include <cstdio>
#include <cstring>

#include "modules.h"
#include "oyster_core/hosts_json.h"

namespace oyster::modules {
namespace {

using hosts::Ctx;
using hosts::Fail;

constexpr uint8_t kCallList = 1;    // arg: "<token>"
constexpr uint8_t kCallHealth = 2;  // arg: "<token>:<host id>"
constexpr uint32_t kLoadTimeoutMs = 25000;  // no reply after this long (e.g. radio busy): show "no answer"
constexpr size_t kRowCols = 27;             // characters of a list row

// Where the user is. The page actually shown also depends on whether data is there (current_page()).
enum class Level : uint8_t { List, Host, Services, Logs };
enum Page : uint8_t { kPageList = 0, kPageOverview, kPageServices, kPageLogs, kPageMessage, kPageHostMessage };

struct Fetch {
  bool busy;
  uint8_t token;  // latest request; 0 = no reply is expected
  uint32_t started_ms;
  Fail fail;      // why the last request failed (None while data is valid)
  int status;
  char detail[96];
};

struct State {
  Level level;
  hosts::HostList list;
  bool list_valid;
  Fetch list_fetch;
  char sel_id[hosts::kIdLen + 1];
  char sel_name[hosts::kNameLen + 1];
  hosts::Health health;
  bool health_valid;
  uint32_t health_ms;   // millis() when the reading arrived
  uint32_t shown_age;   // age in seconds that the last view() drew (tick() redraws when it moves on)
  Fetch health_fetch;
  uint8_t last_token;
};

State s_state;                           // guarded by modules::Guard
char s_resp[hosts::kMaxReply + 2];       // reply buffer, worker task only

uint8_t next_token() {  // caller holds the guard
  s_state.last_token = s_state.last_token == 255 ? 1 : static_cast<uint8_t>(s_state.last_token + 1);
  return s_state.last_token;
}

Page current_page() {  // caller holds the guard
  switch (s_state.level) {
    case Level::List: return (s_state.list_valid && s_state.list.count > 0) ? kPageList : kPageMessage;
    case Level::Host: return s_state.health_valid ? kPageOverview : kPageHostMessage;
    case Level::Services: return s_state.health_valid ? kPageServices : kPageHostMessage;
    case Level::Logs: return kPageLogs;
  }
  return kPageMessage;
}

// Elapsed milliseconds between two millis() readings; `now` may be slightly older than `since` (the loop
// reads the clock before it handles keys), which counts as zero rather than wrapping to 49 days.
uint32_t elapsed_ms(uint32_t now, uint32_t since) {
  const int32_t d = static_cast<int32_t>(now - since);
  return d < 0 ? 0u : static_cast<uint32_t>(d);
}

uint32_t age_seconds(uint32_t now) { return elapsed_ms(now, s_state.health_ms) / 1000u; }

// Redraw when the age text would change: every second in the first minute, then every minute.
uint32_t age_bucket(uint32_t age) { return age < 60u ? age : 60u + age / 60u; }

// ---- requests (UI task) ------------------------------------------------------------------------------------

// Marks a request as started and returns its token. False when one is already running.
bool begin_fetch(Fetch& f, uint8_t& token) {  // caller holds the guard
  if (f.busy) return false;
  token = next_token();
  f = Fetch{};
  f.busy = true;
  f.token = token;
  f.started_ms = millis();
  return true;
}

// The request could not even be queued: keep what is shown, or say so when nothing is.
void abort_fetch(Fetch& f, bool valid) {  // caller holds the guard
  f.busy = false;
  f.token = 0;
  if (!valid) f.fail = Fail::Busy;
}

void fetch_list(bool quiet) {
  char arg[8];
  {
    Guard g;
    uint8_t token = 0;
    if (!begin_fetch(s_state.list_fetch, token)) {
      if (!quiet) toast("Already loading");
      return;
    }
    std::snprintf(arg, sizeof(arg), "%02x", static_cast<unsigned>(token));
  }
  if (!request(Id::Hosts, kCallList, arg)) {
    {
      Guard g;
      abort_fetch(s_state.list_fetch, s_state.list_valid);
    }
    toast("Busy, try again");
  }
  changed();
}

void fetch_health() {
  char arg[48];
  {
    Guard g;
    if (s_state.sel_id[0] == '\0') return;
    uint8_t token = 0;
    if (!begin_fetch(s_state.health_fetch, token)) {
      toast("Already loading");
      return;
    }
    std::snprintf(arg, sizeof(arg), "%02x:%s", static_cast<unsigned>(token), s_state.sel_id);
  }
  if (!request(Id::Hosts, kCallHealth, arg)) {
    {
      Guard g;
      abort_fetch(s_state.health_fetch, s_state.health_valid);
    }
    toast("Busy, try again");
  }
  changed();
}

void open_host(uint16_t row) {
  {
    Guard g;
    if (row >= s_state.list.count) return;
    const hosts::Host& h = s_state.list.hosts[row];
    std::snprintf(s_state.sel_id, sizeof(s_state.sel_id), "%s", h.id);
    std::snprintf(s_state.sel_name, sizeof(s_state.sel_name), "%s", h.name);
    s_state.level = Level::Host;
    s_state.health_valid = false;
    s_state.health_fetch = Fetch{};  // a reply to an earlier request (token) is dropped
  }
  fetch_health();
}

// ---- worker task -------------------------------------------------------------------------------------------

int hex_digit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// "<two hex digits>" at the start of arg; `rest` points behind them.
bool parse_token(const char* arg, uint8_t& token, const char*& rest) {
  if (arg == nullptr) return false;
  const int a = hex_digit(arg[0]);
  const int b = a >= 0 ? hex_digit(arg[1]) : -1;
  if (b < 0) return false;
  token = static_cast<uint8_t>(a * 16 + b);
  rest = arg + 2;
  return true;
}

// The client's own words for what went wrong (no secrets by contract); only kept for link-level failures.
void copy_detail(Fail f, char* out, size_t n) {
  out[0] = '\0';
  if (f == Fail::Offline || f == Fail::Transport || f == Fail::Clock) std::snprintf(out, n, "%s", last_error());
}

void run_list(const char* arg) {
  uint8_t token = 0;
  const char* rest = nullptr;
  if (!parse_token(arg, token, rest)) return;
  size_t n = 0;
  const int st = http("GET", "/v1/hosts", nullptr, 0, s_resp, sizeof(s_resp), &n, true);
  hosts::HostList parsed{};
  Fail fail = hosts::classify_reply(st, Ctx::List, s_resp, n);
  if (fail == Fail::None) {
    fail = n > hosts::kMaxReply ? Fail::TooLarge : hosts::from_parse(hosts::parse_host_list(s_resp, n, parsed));
  }
  char detail[sizeof(Fetch::detail)];
  copy_detail(fail, detail, sizeof(detail));
  {
    Guard g;
    Fetch& f = s_state.list_fetch;
    if (!f.busy || f.token != token) return;  // cancelled, timed out or superseded
    f.busy = false;
    f.status = st;
    f.fail = fail;
    std::snprintf(f.detail, sizeof(f.detail), "%s", detail);
    if (fail == Fail::None) {
      hosts::carry_states(s_state.list, parsed);
      s_state.list = parsed;
      s_state.list_valid = true;
    } else {
      s_state.list = hosts::HostList{};
      s_state.list_valid = false;
    }
  }
  changed();
}

void run_health(const char* arg) {
  uint8_t token = 0;
  const char* rest = nullptr;
  if (!parse_token(arg, token, rest) || rest[0] != ':') return;
  const char* id = rest + 1;
  char path[64];
  hosts::Health parsed{};
  int st = 0;
  Fail fail = Fail::Malformed;
  if (hosts::health_path(id, path, sizeof(path))) {
    size_t n = 0;
    st = http("GET", path, nullptr, 0, s_resp, sizeof(s_resp), &n, true);
    fail = hosts::classify_reply(st, Ctx::Health, s_resp, n);
    if (fail == Fail::None) {
      fail = n > hosts::kMaxReply ? Fail::TooLarge : hosts::from_parse(hosts::parse_health(s_resp, n, id, parsed));
    }
  }
  char detail[sizeof(Fetch::detail)];
  copy_detail(fail, detail, sizeof(detail));
  bool notify = true;
  {
    Guard g;
    // What was learned about this host is useful in the list even if the reply is no longer wanted here.
    const int idx = hosts::find_host(s_state.list, id);
    if (idx >= 0) {
      if (fail == Fail::None) {
        s_state.list.hosts[idx].state = parsed.partial ? hosts::HostState::Partial : hosts::HostState::Ok;
      } else if (fail == Fail::Unreachable || fail == Fail::Timeout) {
        s_state.list.hosts[idx].state = hosts::HostState::Down;
      }
    }
    Fetch& f = s_state.health_fetch;
    if (!f.busy || f.token != token) {
      notify = idx >= 0;  // cancelled, timed out or superseded: only the list row may have changed
    } else {
      f.busy = false;
      f.status = st;
      f.fail = fail;
      std::snprintf(f.detail, sizeof(f.detail), "%s", detail);
      s_state.health_valid = fail == Fail::None;
      if (s_state.health_valid) {
        s_state.health = parsed;
        s_state.health_ms = millis();
        s_state.shown_age = 0;
      }
    }
  }
  if (notify) changed();
}

void mod_call(uint8_t call_id, const char* arg) {
  if (call_id == kCallList) {
    run_list(arg);
  } else if (call_id == kCallHealth) {
    run_health(arg);
  }
}

// ---- pages (UI task) ---------------------------------------------------------------------------------------

void host_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i >= s_state.list.count) {
    if (n > 0) out[0] = '\0';
    return;
  }
  hosts::format_host_row(s_state.list.hosts[i], out, n, kRowCols);
}

void service_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i >= s_state.health.service_count) {
    if (n > 0) out[0] = '\0';
    return;
  }
  hosts::format_service_row(s_state.health.services[i], out, n, kRowCols);
}

void view_list_message(PageView& v) {  // caller holds the guard
  const Fetch& f = s_state.list_fetch;
  v.kind = PageKind::Text;
  if (s_state.list_valid) {  // a list with no host in it
    std::snprintf(v.title, sizeof(v.title), "Hosts");
    std::snprintf(v.body, sizeof(v.body),
                  "No hosts shared with this device.\n\nAsk the backend owner to share a host with it.");
    std::snprintf(v.left, sizeof(v.left), "%s", f.busy ? "Updating... Del:Back" : "Del:Back r:Refresh");
  } else if (f.busy || f.fail == Fail::None) {
    std::snprintf(v.title, sizeof(v.title), "Hosts");
    std::snprintf(v.body, sizeof(v.body), "Loading hosts...\n\nRead-only: nothing is run on any host.");
    std::snprintf(v.left, sizeof(v.left), "Del:Back");
  } else {
    std::snprintf(v.title, sizeof(v.title), "!Hosts: %s", hosts::fail_label(f.fail));
    hosts::fail_text(f.fail, Ctx::List, f.status, f.detail, v.body, sizeof(v.body));
    std::snprintf(v.left, sizeof(v.left), "Ent:Retry Del:Back");
  }
  v.busy = f.busy;
}

void view_host_message(PageView& v) {  // caller holds the guard
  const Fetch& f = s_state.health_fetch;
  v.kind = PageKind::Text;
  if (f.busy || f.fail == Fail::None) {
    std::snprintf(v.title, sizeof(v.title), "%.31s", s_state.sel_name);
    std::snprintf(v.body, sizeof(v.body), "Reading health...\n\nRead-only: nothing is run on the host.");
    std::snprintf(v.left, sizeof(v.left), "Del:Back");
  } else {
    std::snprintf(v.title, sizeof(v.title), "!Host: %s", hosts::fail_label(f.fail));
    const int used = std::snprintf(v.body, sizeof(v.body), "%.40s\n\n", s_state.sel_name);
    if (used > 0 && static_cast<size_t>(used) < sizeof(v.body)) {
      hosts::fail_text(f.fail, Ctx::Health, f.status, f.detail, v.body + used, sizeof(v.body) - used);
    }
    std::snprintf(v.left, sizeof(v.left), "%s", f.fail == Fail::NotFound ? "Ent:List Del:Back" : "Ent:Retry Del:Back");
  }
  v.busy = f.busy;
}

void mod_view(PageView& v) {
  Guard g;
  const Page page = current_page();
  v.page_id = page;
  switch (page) {
    case kPageList:
      v.kind = PageKind::List;
      std::snprintf(v.title, sizeof(v.title), "Hosts");
      v.count = s_state.list.count;
      v.row = host_row;
      std::snprintf(v.left, sizeof(v.left), "%s",
                    s_state.list_fetch.busy ? "Updating... Del:Back" : "Del:Back r:Refresh");
      std::snprintf(v.right, sizeof(v.right), "Ent:Open");
      v.busy = s_state.list_fetch.busy;
      break;
    case kPageMessage: view_list_message(v); break;
    case kPageHostMessage: view_host_message(v); break;
    case kPageOverview: {
      const uint32_t age = age_seconds(millis());
      s_state.shown_age = age;
      v.kind = PageKind::Text;
      std::snprintf(v.title, sizeof(v.title), "%.31s", s_state.sel_name);
      hosts::format_overview(s_state.health, age, v.body, sizeof(v.body));
      std::snprintf(v.left, sizeof(v.left), "%s",
                    s_state.health_fetch.busy ? "Updating... Del:Back" : "Del:Back r:Ref l:Logs");
      v.busy = s_state.health_fetch.busy;
      break;
    }
    case kPageServices:
      v.kind = PageKind::List;
      std::snprintf(v.title, sizeof(v.title), "Services: %.20s", s_state.sel_name);
      v.count = s_state.health.service_count;
      v.row = service_row;
      v.empty = "No services listed";
      std::snprintf(v.left, sizeof(v.left), "%s",
                    s_state.health_fetch.busy ? "Updating... Del:Back" : "Del:Back r:Refresh");
      std::snprintf(v.right, sizeof(v.right), "Ent:Name");
      v.busy = s_state.health_fetch.busy;
      break;
    case kPageLogs:
      v.kind = PageKind::Text;
      std::snprintf(v.title, sizeof(v.title), "Logs: %.24s", s_state.sel_name);
      std::snprintf(v.body, sizeof(v.body),
                    "No log source.\n\nThis host shares read-only health only: CPU, memory, disk, uptime and "
                    "service states. Nothing is read from it or run on it, so there are no logs to show.");
      std::snprintf(v.left, sizeof(v.left), "Del:Back");
      break;
  }
}

void mod_select(uint16_t row) {
  Page page;
  {
    Guard g;
    page = current_page();
  }
  switch (page) {
    case kPageList: open_host(row); break;
    case kPageMessage: fetch_list(false); break;  // retry, or reload an empty list
    case kPageHostMessage: {
      bool gone;
      {
        Guard g;
        gone = s_state.health_fetch.fail == Fail::NotFound;
        if (gone) s_state.level = Level::List;
      }
      if (gone) {
        fetch_list(false);  // the host is no longer shared: show the current list
      } else {
        fetch_health();
      }
      break;
    }
    case kPageOverview: {
      bool has_services;
      {
        Guard g;
        has_services = s_state.health.service_count > 0;
        if (has_services) s_state.level = Level::Services;
      }
      if (!has_services) toast("No services shared");
      break;
    }
    case kPageServices: {
      char text[72] = "";
      {
        Guard g;
        if (row < s_state.health.service_count) {
          const hosts::Service& sv = s_state.health.services[row];
          std::snprintf(text, sizeof(text), "%s: %s", sv.name, hosts::service_state_text(sv.state));
        }
      }
      if (text[0] != '\0') toast(text);
      break;
    }
    case kPageLogs: break;
  }
}

bool mod_back() {
  Guard g;
  if (s_state.level == Level::List) return false;  // leave the module
  if (current_page() == kPageHostMessage || s_state.level == Level::Host) {
    s_state.level = Level::List;
  } else {
    s_state.level = Level::Host;  // services, logs
  }
  return true;
}

bool mod_key(NavEvent e, char c) {
  if (e != NavEvent::Char) return false;
  if (c == 'r' || c == 'R') {
    Level level;
    {
      Guard g;
      level = s_state.level;
    }
    if (level == Level::List) {
      fetch_list(false);
    } else if (level == Level::Logs) {
      return false;  // static page, nothing to refresh
    } else {
      fetch_health();
    }
    return true;
  }
  if (c == 'l' || c == 'L') {
    Guard g;
    if (s_state.level != Level::Host || !s_state.health_valid) return false;
    s_state.level = Level::Logs;
    return true;
  }
  return false;
}

// A request that got no reply: the worker may have refused it (radio busy) or the link is stuck.
bool expire(Fetch& f, bool& valid, uint32_t now) {  // caller holds the guard
  if (!f.busy || elapsed_ms(now, f.started_ms) < kLoadTimeoutMs) return false;
  f.busy = false;
  f.token = 0;  // a late reply is dropped
  f.fail = Fail::NoAnswer;
  f.status = 0;
  f.detail[0] = '\0';
  valid = false;
  return true;
}

bool mod_tick(uint32_t now_ms) {
  Guard g;
  bool dirty = expire(s_state.list_fetch, s_state.list_valid, now_ms);
  if (dirty) s_state.list = hosts::HostList{};
  dirty |= expire(s_state.health_fetch, s_state.health_valid, now_ms);
  if (!dirty && current_page() == kPageOverview) {
    dirty = age_bucket(age_seconds(now_ms)) != age_bucket(s_state.shown_age);
  }
  return dirty;
}

void cancel_fetch(Fetch& f, bool valid) {  // caller holds the guard
  if (!f.busy) return;
  f.busy = false;
  f.token = 0;
  if (!valid) f.fail = Fail::Cancelled;
}

void mod_cancel() {
  {
    Guard g;
    cancel_fetch(s_state.list_fetch, s_state.list_valid);
    cancel_fetch(s_state.health_fetch, s_state.health_valid);
  }
  changed();
}

void mod_open() {
  {
    Guard g;
    // tick() does not run while the module is closed: a request that never completed is dropped here
    if (s_state.list_fetch.busy && elapsed_ms(millis(), s_state.list_fetch.started_ms) >= kLoadTimeoutMs) {
      s_state.list_fetch.busy = false;
      s_state.list_fetch.token = 0;
    }
    s_state.level = Level::List;
    s_state.sel_id[0] = '\0';
    s_state.health_valid = false;
    s_state.health_fetch = Fetch{};
  }
  fetch_list(true);  // an earlier list stays on screen until the new one arrives
}

bool mod_available(const char** why) {
  (void)why;
  return true;  // an offline backend is reported on the page, where it can be retried
}

void mod_tile_status(char* out, size_t n) {
  if (!backend_ready()) {
    std::snprintf(out, n, "offline");
    return;
  }
  Guard g;
  if (s_state.list_valid) {
    hosts::format_host_count(s_state.list.count, s_state.list.more, out, n);
  } else if (s_state.list_fetch.busy) {
    std::snprintf(out, n, "loading");
  } else if (s_state.list_fetch.fail != Fail::None) {
    std::snprintf(out, n, "error");
  } else {
    std::snprintf(out, n, "view hosts");
  }
}

}  // namespace

const Ops kHostsOps = {"Hosts",    mod_available, mod_open, mod_view,   mod_select, mod_back,
                       mod_key,    mod_tick,      mod_cancel, mod_call, mod_tile_status};

}  // namespace oyster::modules
