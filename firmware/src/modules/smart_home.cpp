// Smart Home (Home tile 5, screens SH10-SH12): the IR remote of the Cardputer and Home Assistant through the
// PC backend's allowlist (docs/HOME_ASSISTANT.md).
//
//   Rooms (SH10): "IR Remote", then the Home Assistant rooms, or one muted status row when Home Assistant is
//                 not set up / not reachable (Enter explains what to do)
//   IR:  brands -> keys (Enter or 1-6 sends; the IR LED is driven on the worker task)
//   HA:  room -> devices with their state -> allowed actions (Enter or 1-3 sends; locks, covers and alarm
//        panels first ask on a Confirm page, and the request is only sent after Enter)
//
// Honest by construction: IR has no reply, so the screens say "sent", never "done"; Home Assistant states are
// shown as the backend read them; failures are plain texts. Threading: view/select/key/back/tick run on the UI
// task, call() on the worker; shared state is guarded with modules::Guard and never held across HTTP.
#include <Arduino.h>

#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <new>

#include "ir_remote.h"
#include "modules.h"
#include "oyster_core/home_json.h"
#include "oyster_core/ir_codes.h"

namespace oyster::modules {
namespace {

using homejson::Device;
using homejson::DeviceList;
using homejson::RoomList;

enum class Page : uint8_t { Rooms, IrBrands, IrKeys, HaDevices, HaActions };
enum class Overlay : uint8_t { None, TextHa, TextIr, Confirm };
enum class Ha : uint8_t { Unknown, Loading, NotSet, Ready, Failed };
enum Call : uint8_t { kFetchRooms = 1, kFetchDevices = 2, kSendIr = 3, kHaAction = 4 };

constexpr uint32_t kNetTimeoutMs = 20000;  // a request that never reported back is given up on
constexpr uint32_t kIrTimeoutMs = 3000;
constexpr uint8_t kIrQueueMax = 3;
constexpr size_t kRespCap = 5120;          // the backend client buffer is 5 KiB

struct Want {  // one Home Assistant action, from the key press to the answer
  bool active;
  uint32_t since;
  char entity[homejson::kEntityLen];
  char label[homejson::kNameLen];
  uint8_t action;  // homejson::ActionBit
  bool impactful;
  char key[24];    // Idempotency-Key: one per key press
};

struct State {
  Page page;
  Overlay overlay;
  Ha ha;
  uint32_t ha_since;
  char ha_reason[homejson::kReasonLen];
  char ha_error[48];
  RoomList rooms;
  DeviceList devs;
  bool devs_loading;
  uint32_t devs_since;
  char devs_error[48];
  char fetch_room[homejson::kNameLen];
  uint8_t brand_sel;
  uint8_t dev_sel;
  uint32_t gen_rooms;  // bumped when a request is started or given up on: answers of older requests are dropped
  uint32_t gen_devs;
  Want pending;  // sent, waiting for the answer
  Want ask;      // prepared, waiting on the Confirm page
  uint8_t ir_queued;
  uint32_t ir_since;
};

State s;

// ---- small helpers ---------------------------------------------------------------------------------

void set_text(char* dst, size_t n, const char* src) { std::snprintf(dst, n, "%s", src != nullptr ? src : ""); }

uint8_t action_count(uint8_t mask) {
  uint8_t n = 0;
  for (uint8_t bit : {homejson::kTurnOn, homejson::kTurnOff, homejson::kToggle}) n += (mask & bit) ? 1 : 0;
  return n;
}

uint8_t nth_action(uint8_t mask, uint16_t n) {
  uint16_t seen = 0;
  for (uint8_t bit : {homejson::kTurnOn, homejson::kTurnOff, homejson::kToggle}) {
    if ((mask & bit) == 0) continue;
    if (seen == n) return bit;
    ++seen;
  }
  return 0;
}

bool domain_is(const char* entity, const char* domain) {
  const size_t n = std::strlen(domain);
  return std::strncmp(entity, domain, n) == 0 && entity[n] == '.';
}

// What an action means for this kind of entity (the backend maps the generic action to the right service).
const char* verb(const char* entity, uint8_t bit) {
  if (domain_is(entity, "lock")) return bit == homejson::kTurnOn ? "Lock" : "Unlock";
  if (domain_is(entity, "cover")) return bit == homejson::kTurnOn ? "Open" : (bit == homejson::kTurnOff ? "Close" : "Toggle");
  if (domain_is(entity, "alarm_control_panel")) return "Arm (away)";
  if (domain_is(entity, "scene")) return "Activate";
  return bit == homejson::kTurnOn ? "Turn on" : (bit == homejson::kTurnOff ? "Turn off" : "Toggle");
}

const char* reason_text(const char* reason) {
  if (std::strcmp(reason, "no_url") == 0) return "Missing: HOME_ASSISTANT_URL";
  if (std::strcmp(reason, "no_token") == 0) return "Missing: HOME_ASSISTANT_TOKEN";
  if (std::strcmp(reason, "bad_url") == 0) return "HOME_ASSISTANT_URL is not a plain http(s) URL";
  if (std::strcmp(reason, "bad_token") == 0) return "HOME_ASSISTANT_TOKEN has a bad format";
  if (std::strcmp(reason, "no_allowlist") == 0) return "Missing: the allowlist file";
  if (std::strcmp(reason, "bad_allowlist") == 0) return "The allowlist file is invalid";
  if (std::strcmp(reason, "no_rooms") == 0) return "The allowlist has no rooms";
  return "Not configured";
}

// Plain text for an error answer of the backend (status and, when present, its error code).
void describe_error(int st, const char* resp, size_t n, char* out, size_t cap) {
  homejson::ErrorBody e;
  const bool parsed = st > 0 && homejson::parse_error(resp, n, &e);
  struct Row {
    const char* code;
    const char* text;
  };
  static const Row kRows[] = {
      {"home_not_configured", "Home Assistant is not set up"},
      {"home_unreachable", "Home Assistant is unreachable"},
      {"home_timeout", "Home Assistant timed out; result unknown"},
      {"home_auth_failed", "Home Assistant rejected the token"},
      {"home_upstream_error", "Home Assistant returned an error"},
      {"home_refused", "Home Assistant refused the action"},
      {"confirmation_required", "Needs confirmation; not sent"},
      {"not_found", "Not in the allowlist"},
      {"action_not_allowed", "Action not allowed here"},
      {"rate_limited", "Too many requests; wait a moment"},
      {"unauthorized", "Backend refused this device"},
      {"forbidden", "Backend refused this device"},
      {"validation_failed", "Request rejected by the backend"},
      {"idempotency_conflict", "Request clash; try again"},
      {"request_in_progress", "The same request is still running"},
      {"home_entity_missing", "Home Assistant does not know it"},
      {"audit_unavailable", "Backend cannot log it; not done"},
      {"payload_too_large", "Request rejected by the backend"},
  };
  if (parsed) {
    for (const Row& r : kRows) {
      if (std::strcmp(e.code, r.code) == 0) {
        set_text(out, cap, r.text);
        return;
      }
    }
  }
  if (st <= 0) set_text(out, cap, "No answer from the PC backend");
  else if (st == 401 || st == 403) set_text(out, cap, "Backend refused this device");
  else if (st == 404) set_text(out, cap, "Backend has no Smart Home API");
  else if (st == 429) set_text(out, cap, "Too many requests; wait a moment");
  else std::snprintf(out, cap, "Backend error %d", st);
}

// ---- page rows (UI task) -------------------------------------------------------------------------------

uint16_t room_rows() { return s.ha == Ha::Ready && s.rooms.count > 0 ? s.rooms.count : 1; }

void rooms_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i == 0) {
    std::snprintf(out, n, "IR Remote (%u TVs)", static_cast<unsigned>(ircodes::brand_count()));
    return;
  }
  if (s.ha == Ha::Ready && i - 1 < s.rooms.count) {
    std::snprintf(out, n, "%.20s (%u)", s.rooms.rooms[i - 1].name, static_cast<unsigned>(s.rooms.rooms[i - 1].count));
    return;
  }
  switch (s.ha) {
    case Ha::Unknown: std::snprintf(out, n, "Home Assistant: no backend"); break;
    case Ha::Loading: std::snprintf(out, n, "Home Assistant: loading"); break;
    case Ha::NotSet: std::snprintf(out, n, "Home Assistant: not set up"); break;
    case Ha::Failed: std::snprintf(out, n, "Home Assistant: offline"); break;
    case Ha::Ready: std::snprintf(out, n, "Home Assistant: no rooms"); break;
  }
}

bool rooms_disabled(uint16_t i) {
  Guard g;
  return i >= 1 && !(s.ha == Ha::Ready && s.rooms.count > 0);
}

void brands_row(uint16_t i, char* out, size_t n) {
  const ircodes::Brand* b = ircodes::brand_at(i);
  std::snprintf(out, n, "%s", b != nullptr ? b->name : "About IR codes");
}

void keys_row(uint16_t i, char* out, size_t n) {
  std::snprintf(out, n, "%u %s", static_cast<unsigned>(i + 1), ircodes::func_label(static_cast<ircodes::Func>(i)));
}

void devices_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i >= s.devs.count) {
    if (n > 0) out[0] = '\0';
    return;
  }
  const Device& d = s.devs.devices[i];
  const char* st = std::strcmp(d.state, "unavailable") == 0 ? "unavail" : d.state;
  std::snprintf(out, n, "%-17.17s%c %9.9s", d.label, d.impactful ? '!' : ' ', st);
}

void actions_row(uint16_t i, char* out, size_t n) {
  Guard g;
  if (s.dev_sel >= s.devs.count) {
    if (n > 0) out[0] = '\0';
    return;
  }
  const Device& d = s.devs.devices[s.dev_sel];
  const uint8_t bit = nth_action(d.actions, i);
  std::snprintf(out, n, "%u %s", static_cast<unsigned>(i + 1), bit != 0 ? verb(d.entity_id, bit) : "");
}

void text_view(PageView& v) {
  v.kind = PageKind::Text;
  std::snprintf(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
  if (s.overlay == Overlay::TextIr) {
    v.page_id = 7;
    std::snprintf(v.title, sizeof(v.title), "About IR codes");
    std::snprintf(v.body, sizeof(v.body),
                  "The codes are fixed tables for common TV remote families (NEC, Samsung32, Sony SIRC, RC5) "
                  "from public protocol documentation.\nThey are NOT verified on your devices, and the IR LED pin "
                  "(GPIO44) is unverified on this unit.\nIR is one-way: nothing comes back, so this screen cannot "
                  "know whether the TV obeyed.\nPoint the device at the TV. There is no learn mode (no IR receiver).");
    return;
  }
  v.page_id = 5;
  switch (s.ha) {
    case Ha::NotSet:
      std::snprintf(v.title, sizeof(v.title), "Home Assistant: not set up");
      std::snprintf(v.body, sizeof(v.body),
                    "%s.\nOn the PC, backend/.env needs HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN, and "
                    "OYSTER_HOME_ALLOWLIST names the JSON file with the rooms and entities you allow (default: "
                    "home_allowlist.json in the data folder). Restart the backend after editing.\n"
                    "Details: docs/HOME_ASSISTANT.md.\nIR Remote works without any of this.",
                    reason_text(s.ha_reason));
      break;
    case Ha::Failed:
      std::snprintf(v.title, sizeof(v.title), "!Home Assistant: offline");
      std::snprintf(v.body, sizeof(v.body),
                    "%s.\nPress r on the room list to try again.\nIR Remote works without it.", s.ha_error);
      break;
    case Ha::Loading:
      std::snprintf(v.title, sizeof(v.title), "Home Assistant");
      std::snprintf(v.body, sizeof(v.body), "Asking the PC backend for the rooms...");
      break;
    case Ha::Unknown:
    case Ha::Ready:
      std::snprintf(v.title, sizeof(v.title), "Home Assistant: no backend");
      std::snprintf(v.body, sizeof(v.body),
                    "Not connected to the PC backend, so Home Assistant cannot be asked.\nCheck Settings > Backend, "
                    "then press r on the room list.\nIR Remote works without it.");
      break;
  }
}

void confirm_view(PageView& v) {
  v.kind = PageKind::Confirm;
  v.page_id = 6;
  std::snprintf(v.title, sizeof(v.title), "!Confirm");
  std::snprintf(v.confirm_operation, sizeof(v.confirm_operation), "%s", verb(s.ask.entity, s.ask.action));
  std::snprintf(v.confirm_target, sizeof(v.confirm_target), "%s", s.ask.label);
  std::snprintf(v.confirm_effect, sizeof(v.confirm_effect),
                "Sent to Home Assistant at once. Check the result yourself. Enter: do it. Del: cancel.");
  std::snprintf(v.left, sizeof(v.left), "Ent:Do it Del:Cancel");
}

// ---- actions started on the UI task (caller holds the Guard) ---------------------------------------------------

void start_rooms_fetch() {
  s.ha = Ha::Loading;
  s.ha_since = millis();
  ++s.gen_rooms;
  if (!request(Id::SmartHome, kFetchRooms)) {
    s.ha = Ha::Failed;
    set_text(s.ha_error, sizeof(s.ha_error), "Busy; press r to retry");
  }
}

void load_devices() {
  s.devs_loading = true;
  s.devs_since = millis();
  s.devs_error[0] = '\0';
  ++s.gen_devs;
  if (!request(Id::SmartHome, kFetchDevices)) {
    s.devs_loading = false;
    set_text(s.devs_error, sizeof(s.devs_error), "Busy; press r to retry");
  }
}

void open_room(uint8_t idx) {
  s.page = Page::HaDevices;
  s.dev_sel = 0;
  s.devs.count = 0;
  s.devs.more = false;
  s.devs.next_after[0] = '\0';
  set_text(s.devs.room_id, sizeof(s.devs.room_id), s.rooms.rooms[idx].id);
  set_text(s.devs.room_name, sizeof(s.devs.room_name), s.rooms.rooms[idx].name);
  set_text(s.fetch_room, sizeof(s.fetch_room), s.rooms.rooms[idx].id);
  load_devices();
}

void send_ir(uint8_t func) {
  if (s.ir_queued >= kIrQueueMax) {
    toast("IR is still sending");
    return;
  }
  char arg[16];
  std::snprintf(arg, sizeof(arg), "%u,%u", static_cast<unsigned>(s.brand_sel), static_cast<unsigned>(func));
  if (!request(Id::SmartHome, kSendIr, arg)) {
    toast("Busy; try again");
    return;
  }
  ++s.ir_queued;
  s.ir_since = millis();
}

void send_ask() {
  if (s.pending.active) {
    toast("Wait for the last answer");
    s.ask.active = false;
    return;
  }
  s.pending = s.ask;
  s.ask.active = false;
  s.pending.active = true;
  s.pending.since = millis();
  std::snprintf(s.pending.key, sizeof(s.pending.key), "sh-%08lx%08lx", static_cast<unsigned long>(esp_random()),
                static_cast<unsigned long>(esp_random()));
  if (!request(Id::SmartHome, kHaAction)) {
    s.pending.active = false;
    toast("Busy; try again");
  }
}

void start_action_row(uint16_t row) {
  if (s.dev_sel >= s.devs.count) return;
  const Device& d = s.devs.devices[s.dev_sel];
  const uint8_t bit = nth_action(d.actions, row);
  if (bit == 0) return;
  if (s.pending.active) {
    toast("Wait for the last answer");
    return;
  }
  Want& a = s.ask;
  a.active = true;
  set_text(a.entity, sizeof(a.entity), d.entity_id);
  set_text(a.label, sizeof(a.label), d.label);
  a.action = bit;
  a.impactful = d.impactful;
  if (d.impactful) {
    s.overlay = Overlay::Confirm;  // nothing is sent before Enter on the Confirm page
    return;
  }
  send_ask();
}

void refresh() {
  if (s.page == Page::Rooms) {
    if (!backend_ready()) {
      s.ha = Ha::Unknown;
      toast("Not connected to the PC backend");
      return;
    }
    start_rooms_fetch();
  } else if (s.page == Page::HaDevices) {
    if (!backend_ready()) {
      toast("Not connected to the PC backend");
      return;
    }
    s.devs.count = 0;
    load_devices();
  }
}

// ---- Ops (UI task) -----------------------------------------------------------------------------------------

bool available(const char** why) {
  (void)why;
  return true;  // the IR remote needs nothing else
}

void open() {
  Guard g;
  s.page = Page::Rooms;
  s.overlay = Overlay::None;
  s.pending.active = false;
  s.ask.active = false;
  s.ir_queued = 0;
  s.devs.count = 0;
  s.devs_loading = false;
  s.devs_error[0] = '\0';
  s.rooms.count = 0;
  s.rooms.configured = false;
  s.brand_sel = 0;
  s.dev_sel = 0;
  ++s.gen_devs;
  if (backend_ready()) {
    start_rooms_fetch();
  } else {
    s.ha = Ha::Unknown;
    ++s.gen_rooms;
  }
}

void view(PageView& v) {
  Guard g;
  if (s.overlay == Overlay::Confirm) {
    confirm_view(v);
    return;
  }
  if (s.overlay != Overlay::None) {
    text_view(v);
    return;
  }
  v.kind = PageKind::List;
  const bool net_busy = s.pending.active || s.ha == Ha::Loading || (s.page == Page::HaDevices && s.devs_loading);
  const bool ir_busy = s.ir_queued > 0;
  v.busy = net_busy || ir_busy;
  std::snprintf(v.right, sizeof(v.right), "%s", ir_busy ? "sending" : (net_busy ? "working" : ""));
  switch (s.page) {
    case Page::Rooms:
      v.page_id = 0;
      std::snprintf(v.title, sizeof(v.title), "Smart Home");
      v.count = static_cast<uint16_t>(1 + room_rows());
      v.row = rooms_row;
      v.row_disabled = rooms_disabled;
      std::snprintf(v.left, sizeof(v.left), "Ent:Open r:Refresh");
      break;
    case Page::IrBrands:
      v.page_id = 1;
      std::snprintf(v.title, sizeof(v.title), "IR Remote");
      v.count = static_cast<uint16_t>(ircodes::brand_count() + 1);
      v.row = brands_row;
      std::snprintf(v.left, sizeof(v.left), "Ent:Open Del:Back");
      break;
    case Page::IrKeys: {
      v.page_id = 2;
      const ircodes::Brand* b = ircodes::brand_at(s.brand_sel);
      std::snprintf(v.title, sizeof(v.title), "%s", b != nullptr ? b->name : "IR Remote");
      v.count = static_cast<uint16_t>(ircodes::kFuncCount);
      v.row = keys_row;
      std::snprintf(v.left, sizeof(v.left), "Ent/1-6:Send Del:Back");
      break;
    }
    case Page::HaDevices:
      v.page_id = 3;
      std::snprintf(v.title, sizeof(v.title), "%s", s.devs.room_name);
      v.count = s.devs.count;
      v.row = devices_row;
      v.empty = s.devs_loading ? "Loading..." : (s.devs_error[0] != '\0' ? s.devs_error : "No devices in this room");
      std::snprintf(v.left, sizeof(v.left), "Ent:Open r:Refresh");
      break;
    case Page::HaActions: {
      v.page_id = 4;
      if (s.dev_sel < s.devs.count) {
        const Device& d = s.devs.devices[s.dev_sel];
        std::snprintf(v.title, sizeof(v.title), "%.18s: %.10s", d.label, d.state);
        v.count = action_count(d.actions);
      }
      v.row = actions_row;
      v.empty = "No actions allowed";
      std::snprintf(v.left, sizeof(v.left), "Ent/1-3:Do Del:Back");
      break;
    }
  }
}

void select(uint16_t row) {
  Guard g;
  if (s.overlay == Overlay::TextHa || s.overlay == Overlay::TextIr) {
    s.overlay = Overlay::None;
    return;
  }
  if (s.overlay == Overlay::Confirm) {
    s.overlay = Overlay::None;
    if (s.ask.active) send_ask();
    return;
  }
  switch (s.page) {
    case Page::Rooms:
      if (row == 0) {
        s.page = Page::IrBrands;
      } else if (s.ha == Ha::Ready && row - 1 < s.rooms.count) {
        open_room(static_cast<uint8_t>(row - 1));
      } else {
        s.overlay = Overlay::TextHa;
      }
      break;
    case Page::IrBrands:
      if (row < ircodes::brand_count()) {
        s.brand_sel = static_cast<uint8_t>(row);
        s.page = Page::IrKeys;
      } else {
        s.overlay = Overlay::TextIr;
      }
      break;
    case Page::IrKeys:
      if (row < ircodes::kFuncCount) send_ir(static_cast<uint8_t>(row));
      break;
    case Page::HaDevices:
      if (row < s.devs.count) {
        s.dev_sel = static_cast<uint8_t>(row);
        s.page = Page::HaActions;
      }
      break;
    case Page::HaActions:
      start_action_row(row);
      break;
  }
}

bool back() {
  Guard g;
  if (s.overlay != Overlay::None) {
    s.overlay = Overlay::None;  // on the Confirm page this rejects: nothing was sent
    s.ask.active = false;
    return true;
  }
  switch (s.page) {
    case Page::Rooms: return false;
    case Page::IrBrands: s.page = Page::Rooms; return true;
    case Page::IrKeys: s.page = Page::IrBrands; return true;
    case Page::HaDevices:
      s.page = Page::Rooms;
      ++s.gen_devs;  // an answer that is still on its way is no longer wanted
      s.devs_loading = false;
      return true;
    case Page::HaActions: s.page = Page::HaDevices; return true;
  }
  return false;
}

bool key(NavEvent e, char c) {
  if (e != NavEvent::Char) return false;
  Guard g;
  if (s.overlay != Overlay::None) return false;
  if (c == 'r' || c == 'R') {
    refresh();
    return true;
  }
  if (c < '1' || c > '9') return false;
  const uint16_t n = static_cast<uint16_t>(c - '1');
  if (s.page == Page::IrKeys) {
    if (n < ircodes::kFuncCount) send_ir(static_cast<uint8_t>(n));
    return true;
  }
  if (s.page == Page::HaActions) {
    start_action_row(n);
    return true;
  }
  return false;
}

bool tick(uint32_t now) {
  Guard g;
  bool dirty = false;
  if (s.pending.active && static_cast<uint32_t>(now - s.pending.since) > kNetTimeoutMs) {
    s.pending.active = false;  // the answer never came; a late request finds nothing pending and does nothing
    toast("No answer. Check the device state");
    dirty = true;
  }
  if (s.ir_queued > 0 && static_cast<uint32_t>(now - s.ir_since) > kIrTimeoutMs) {
    s.ir_queued = 0;
    dirty = true;
  }
  if (s.ha == Ha::Loading && static_cast<uint32_t>(now - s.ha_since) > kNetTimeoutMs) {
    s.ha = Ha::Failed;
    ++s.gen_rooms;
    set_text(s.ha_error, sizeof(s.ha_error), "No answer from the PC backend");
    dirty = true;
  }
  if (s.devs_loading && static_cast<uint32_t>(now - s.devs_since) > kNetTimeoutMs) {
    s.devs_loading = false;
    ++s.gen_devs;
    set_text(s.devs_error, sizeof(s.devs_error), "No answer. Press r to retry");
    dirty = true;
  }
  return dirty;
}

void cancel() {
  Guard g;
  s.overlay = Overlay::None;  // rejects a Confirm page; requests already sent cannot be recalled
  s.ask.active = false;
}

void tile_status(char* out, size_t n) {
  Guard g;
  std::snprintf(out, n, "%s", s.ha == Ha::Ready ? "IR+HA" : (s.ha == Ha::Failed ? "offline" : "IR only"));
}

// ---- worker task ------------------------------------------------------------------------------------------------

struct Work {  // heap, per call: the response buffer and the parsed lists
  char resp[kRespCap];
  RoomList rooms;
  DeviceList devs;
};

void fetch_rooms() {
  uint32_t gen;
  {
    Guard g;
    gen = s.gen_rooms;
  }
  Work* w = new (std::nothrow) Work();
  Ha result = Ha::Failed;
  char err[48] = "Out of memory";
  char reason[homejson::kReasonLen] = {};
  if (w != nullptr) {
    size_t n = 0;
    const int st = http("GET", "/v1/home/rooms?limit=16", nullptr, 0, w->resp, kRespCap, &n, true);
    if (st == 200) {
      if (homejson::parse_rooms(w->resp, n, &w->rooms) == homejson::Parse::Ok) {
        if (w->rooms.configured && w->rooms.count > 0) {
          result = Ha::Ready;
        } else {
          result = Ha::NotSet;
          set_text(reason, sizeof(reason), w->rooms.configured ? "no_rooms" : w->rooms.reason);
        }
      } else {
        set_text(err, sizeof(err), "Unreadable answer from the backend");
      }
    } else {
      describe_error(st, w->resp, n, err, sizeof(err));
    }
  }
  {
    Guard g;
    if (gen == s.gen_rooms) {  // otherwise the screen was reopened or the request given up on
      s.ha = result;
      if (result == Ha::Ready) s.rooms = w->rooms;
      else s.rooms.count = 0;
      set_text(s.ha_reason, sizeof(s.ha_reason), reason);
      set_text(s.ha_error, sizeof(s.ha_error), err);
    }
  }
  delete w;
  changed();
}

bool all_digits(const char* t) {
  if (t[0] == '\0') return false;
  for (; *t != '\0'; ++t) {
    if (*t < '0' || *t > '9') return false;
  }
  return true;
}

void fetch_devices() {
  uint32_t gen;
  char room[homejson::kNameLen];
  {
    Guard g;
    gen = s.gen_devs;
    set_text(room, sizeof(room), s.fetch_room);
  }
  Work* w = new (std::nothrow) Work();
  char err[48] = "Out of memory";
  bool got = false;
  bool partial = false;
  if (w != nullptr) {
    err[0] = '\0';
    char cursor[homejson::kCursorLen] = {};
    for (int page = 0; page < 3; ++page) {
      char path[112];
      if (cursor[0] != '\0') {
        std::snprintf(path, sizeof(path), "/v1/home/rooms/%s/devices?limit=8&after=%s", room, cursor);
      } else {
        std::snprintf(path, sizeof(path), "/v1/home/rooms/%s/devices?limit=8", room);
      }
      size_t n = 0;
      const int st = http("GET", path, nullptr, 0, w->resp, kRespCap, &n, true);
      if (st != 200) {
        describe_error(st, w->resp, n, err, sizeof(err));
        partial = got;
        break;
      }
      if (homejson::parse_devices(w->resp, n, &w->devs, page > 0) != homejson::Parse::Ok) {
        set_text(err, sizeof(err), "Unreadable answer from the backend");
        partial = got;
        break;
      }
      got = true;
      if (!w->devs.more || !all_digits(w->devs.next_after)) break;
      set_text(cursor, sizeof(cursor), w->devs.next_after);
    }
  }
  {
    Guard g;
    if (gen == s.gen_devs) {
      s.devs_loading = false;
      if (got) {
        s.devs = w->devs;
        s.devs_error[0] = '\0';
      } else {
        s.devs.count = 0;
        set_text(s.devs_error, sizeof(s.devs_error), err);
      }
    }
  }
  delete w;
  if (partial) toast("Some devices could not be loaded");
  changed();
}

void run_ir(const char* arg) {
  unsigned b = 0, f = 0;
  ircodes::Frame frame;
  const ircodes::Brand* brand = nullptr;
  bool sent = false;
  if (arg != nullptr && std::sscanf(arg, "%u,%u", &b, &f) == 2 && f < ircodes::kFuncCount) {
    brand = ircodes::brand_at(b);
    if (ircodes::frame_for(brand, static_cast<ircodes::Func>(f), &frame)) sent = ir::send(frame);
  }
  {
    Guard g;
    if (s.ir_queued > 0) --s.ir_queued;
  }
  char msg[48];
  if (sent) std::snprintf(msg, sizeof(msg), "Sent: %s (IR, no reply)", ircodes::func_label(static_cast<ircodes::Func>(f)));
  else std::snprintf(msg, sizeof(msg), "IR send failed");
  toast(msg);
  changed();
}

void run_ha_action() {
  Want w;
  {
    Guard g;
    if (!s.pending.active) return;  // given up on (timeout, reopened): do nothing
    w = s.pending;
  }
  char body[160];
  const int blen = std::snprintf(body, sizeof(body), "{\"entity_id\":\"%s\",\"action\":\"%s\",\"confirmed\":%s}", w.entity,
                                 homejson::action_name(w.action), w.impactful ? "true" : "false");
  char* resp = new (std::nothrow) char[1024];
  char msg[72];
  homejson::ActionResult res;
  bool ok = false;
  if (resp == nullptr || blen <= 0 || static_cast<size_t>(blen) >= sizeof(body)) {
    std::snprintf(msg, sizeof(msg), "Out of memory; not sent");
  } else {
    size_t n = 0;
    // not idempotent on the client side: a dead keep-alive connection is never retried automatically
    const int st = http("POST", "/v1/home/actions", body, static_cast<size_t>(blen), resp, 1024, &n, false, w.key);
    if (st == 200 && homejson::parse_action(resp, n, &res) == homejson::Parse::Ok) {
      ok = true;
      std::snprintf(msg, sizeof(msg), "%.24s: %s", w.label, res.state[0] != '\0' ? res.state : "sent");
    } else if (st == 200) {
      std::snprintf(msg, sizeof(msg), "Answer not understood; check the state");
    } else if (st <= 0) {
      std::snprintf(msg, sizeof(msg), "No answer; result unknown. Check it");
    } else {
      char why[48];
      describe_error(st, resp, n, why, sizeof(why));
      std::snprintf(msg, sizeof(msg), "Not done: %s", why);
    }
  }
  delete[] resp;
  {
    Guard g;
    if (s.pending.active && std::strcmp(s.pending.key, w.key) == 0) s.pending.active = false;
    if (ok) {
      for (uint8_t i = 0; i < s.devs.count; ++i) {
        if (std::strcmp(s.devs.devices[i].entity_id, res.entity_id) == 0) {
          set_text(s.devs.devices[i].state, sizeof(s.devs.devices[i].state), res.state);
        }
      }
    }
  }
  toast(msg);
  changed();
}

void call(uint8_t call_id, const char* arg) {
  switch (call_id) {
    case kFetchRooms: fetch_rooms(); break;
    case kFetchDevices: fetch_devices(); break;
    case kSendIr: run_ir(arg); break;
    case kHaAction: run_ha_action(); break;
    default: break;
  }
}

}  // namespace

const Ops kSmartHomeOps = {"Smart Home", available, open, view, select, back, key, tick, cancel, call, tile_status};

}  // namespace oyster::modules
