// Module framework: the Home tiles that talk to the PC backend (Hosts, Control Deck, Smart Home) or to the
// bootloader (Original Apps) are small tables of function pointers in src/modules/<name>.cpp. The UI shows one
// page of the current module at a time (a list, a scrolling text or a confirmation) and calls the module's
// handlers for keys. Network calls run on the worker task (RequestKind::Module), never on the UI task.
//
// Threading: view(), select(), back(), key(), tick(), cancel(), open() and tile_status() run on the UI task;
// call() runs on the worker task. State shared between them is guarded with modules::Guard (a short mutex:
// held while copying data, never across an HTTP request).
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/input_map.h"

namespace oyster::modules {

enum class Id : uint8_t { Hosts, Deck, SmartHome, Original, Count };
constexpr uint8_t kCount = static_cast<uint8_t>(Id::Count);

enum class PageKind : uint8_t {
  List,     // `count` rows drawn through `row`; Enter calls select(row), Back calls back()
  Text,     // `body`, scrolled by the UI; Enter calls select(0), Back calls back()
  Confirm,  // operation / target / effect; Enter approves (select(0)), Back rejects (back()); keys are ignored
            // for the first 700 ms after the page appears (a key pressed before it was readable never answers it)
};

// What the UI draws for the current page. Filled by Ops::view() on every redraw: cheap, no I/O, no allocation.
struct PageView {
  PageKind kind = PageKind::List;
  uint8_t page_id = 0;  // changes when the page changes: the UI then resets selection and scroll
  char title[32] = {};  // leading '!' = alert heading
  uint16_t count = 0;   // List: number of rows
  void (*row)(uint16_t i, char* out, size_t n) = nullptr;
  bool (*row_disabled)(uint16_t i) = nullptr;
  const char* empty = nullptr;  // List with count == 0
  char body[560] = {};          // Text: the text (wrapped and scrolled by the UI)
  char confirm_operation[40] = {};
  char confirm_target[40] = {};
  char confirm_effect[96] = {};
  char left[32] = {};   // action strip, "Key:Action ..." (leading '!' = alert style)
  char right[20] = {};
  bool busy = false;    // a backend call is running (the strip shows "working")
};

struct Ops {
  const char* name;                                    // "Hosts", shown in Help and in messages
  bool (*available)(const char** why);                 // false: the tile opens a message with *why instead
  void (*open)();                                      // entering from Home: reset to the entry page, start the first fetch
  void (*view)(PageView& v);                           // fill the view of the current page
  void (*select)(uint16_t row);                        // Enter on a list row / text page / confirm page
  bool (*back)();                                      // true: handled (popped a page, rejected a confirmation)
  bool (*key)(NavEvent e, char c);                     // extra keys ('r' refresh, '1'..'9'); true: handled
  bool (*tick)(uint32_t now_ms);                       // true: the view changed, redraw
  void (*cancel)();                                    // emergency cancel pressed while this module is open
  void (*call)(uint8_t call_id, const char* arg);      // runs on the worker task: HTTPS + parsing
  void (*tile_status)(char* out, size_t n);            // Home tile detail line, at most n-1 characters
};

// Implemented in src/modules/*.cpp.
extern const Ops kHostsOps;
extern const Ops kDeckOps;
extern const Ops kSmartHomeOps;
extern const Ops kOriginalOps;

const Ops& ops(Id id);
const char* name(Id id);

// ---- framework services for the modules ---------------------------------------------------------------

void begin();  // creates the lock; called once at start-up
void lock();
void unlock();
struct Guard {
  Guard() { lock(); }
  ~Guard() { unlock(); }
  Guard(const Guard&) = delete;
  Guard& operator=(const Guard&) = delete;
};

// Asks the worker to run Ops::call(call_id, arg) of module `id`. False when the request queue is full.
bool request(Id id, uint8_t call_id, const char* arg = nullptr);
// Tells the UI that module data changed (any task): the current page is redrawn, the Home tile text refreshed.
void changed();
// Notice in the action strip (any task).
void toast(const char* text);
// True when the device has a backend configuration and the backend client is not in a failed state.
bool backend_ready();
// One authenticated request to the backend (worker task only). Returns the HTTP status, or a negative value
// when the transport failed or the response was too large. The response is copied into `resp` (NUL-terminated,
// truncated at cap-1). Only paths under /v1/ are accepted. `idempotent` allows one automatic retry on a dead
// keep-alive connection: pass false for anything that must not run twice.
int http(const char* method, const char* path, const char* body, size_t body_len, char* resp, size_t cap,
         size_t* resp_len, bool idempotent, const char* idempotency_key = nullptr);
// The last short error text of the backend client (for messages).
const char* last_error();

// ---- called by the UI (app_controller.cpp) ---------------------------------------------------------------------

void open(Id id);
void view(Id id, PageView& v);
void select(Id id, uint16_t row);
bool back(Id id);
bool key(Id id, NavEvent e, char c);
bool tick(Id id, uint32_t now_ms);
void cancel(Id id);
void run_call(uint8_t module_id, uint8_t call_id, const char* arg);  // worker task
void tile_status(Id id, char* out, size_t n);

}  // namespace oyster::modules
