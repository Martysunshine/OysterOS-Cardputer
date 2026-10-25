// Control Deck (Home tile 4, screens D10-D22): macro profiles of the PC backend, run from the Cardputer.
//
//   Profiles (D10)  -> Slots (slot number + label, '*' = asks first; keys 1-9 or Enter run a slot)
//   Run             -> POST .../slots/{n}/run with a fresh Idempotency-Key per key press, never retried by itself
//   Confirm (D20)   -> impactful slots only: operation / target / effect, bound to the job id and the nonce the
//                      backend returned for THIS device. Enter approves, Back rejects, 120 s at most.
//   Result (D21)    -> polls GET .../jobs/{id} until the job ends; says plainly what the backend reported
//   History (D22)   -> the last 10 backend entries, finished jobs only; Enter opens a job
//
// Honest by construction: a request whose answer was lost is shown as "status unknown" and can only be checked
// again (same key, so it never runs twice); an "unknown outcome" job says the action may have run; the mock
// executor says nothing was run on the PC. The nonce lives in RAM only, is never shown or logged and is wiped
// once used. Threading: view/select/key/back/tick/cancel run on the UI task, call() on the worker; shared state
// is guarded with modules::Guard and never held across HTTP. A late answer of an abandoned request is dropped by
// a generation tag carried in the request argument.
//
// Claude Code row (docs/CLAUDE_CODE_LINK.md, docs/CONTROL_DECK.md "Claude Code row"): the first page has one more
// row after the profiles. It leads to the pending "may Claude Code run this tool?" requests (GET
// /v1/claude/approvals, refreshed every 3 s while that page is open, never while another page or module is
// open), a Confirm page per request (Enter = allow once, Back = deny; POST .../decide, sent once, never
// retried) and a usage page (GET /v1/claude/status). Its nonces live in RAM only and are wiped when used.
#include <Arduino.h>
#include <esp_system.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>

#include "modules.h"
#include "oyster_core/claude_json.h"
#include "oyster_core/deck_json.h"

namespace oyster::modules {
namespace {

namespace dk = oyster::deck;
namespace cl = oyster::claude;

enum class Page : uint8_t {
  Profiles = 1, Slots, Working, Confirm, Result, History,
  ClaudeList, ClaudeConfirm, ClaudeResult, ClaudeStatus,  // the Claude Code row
};
// What the Result page shows.
enum class Kind : uint8_t {
  Loading,     // opening a job from History: asking the backend
  Approving,   // approval sent, waiting for the backend's answer
  Cancelling,  // rejection sent
  Job,         // s.job is valid
  Error,       // the request was refused or never sent: s.err says what
  RunUnknown,  // the run request got no usable answer: Enter checks with the same key
  CheckLater,  // lost contact with a job in flight: Enter asks the backend again
};
enum class Load : uint8_t { Idle, Loading, Ready, Failed };
enum Call : uint8_t {
  kCallProfiles = 1, kCallRun = 2, kCallConfirm = 3, kCallCancel = 4, kCallPoll = 5, kCallHistory = 6,
  kCallClaudeList = 7, kCallClaudeStatus = 8, kCallClaudeDecide = 9,
};

constexpr uint8_t kMaxListPages = 8;       // 8 pages of 5 profiles; the backend holds at most 32
constexpr uint32_t kNetTimeoutMs = 35000;  // a request that never reported back is given up on
constexpr uint32_t kPollMs = 1500;
constexpr uint16_t kMaxPolls = 100;        // about 2.5 minutes, longer than any job budget
constexpr uint8_t kMaxPollFailures = 3;
constexpr uint32_t kBackgroundRetryMs = 30000;
constexpr size_t kRespCap = 5120;          // the backend client buffer is 5 KiB; replies are < 4 KiB
constexpr uint32_t kClaudeRefreshMs = 3000;      // approvals page: how often the list is asked again
constexpr uint32_t kClaudeStatusMs = 15000;      // approvals page: usage line, a slower cadence
constexpr uint32_t kClaudeStatusPageMs = 10000;  // usage page
constexpr size_t kClaudeResp = 2048;             // approvals reply is <= about 1.7 KiB (5 items, escaped summaries)

struct Act {  // the job a confirm/cancel request is about, fixed when the key is pressed
  char id[dk::kTokenCap];
  char nonce[dk::kNonceCap];
};

struct State {
  Page page;
  uint8_t token;               // generation of the run/confirm/poll flow; answers of an older one are dropped
  uint8_t inflight;
  uint32_t inflight_since;

  dk::ProfilePage profiles;    // the page of profiles last fetched
  bool have_profiles;
  Load list;
  bool list_offline;           // the last fetch failed on the network, not on the backend
  char list_error[32];
  uint8_t page_idx;            // index of the page shown
  char cursors[kMaxListPages][dk::kIdCap];  // cursors[i] = `after` that fetches page i ("" for page 0)
  uint8_t list_serial;
  uint8_t list_gen;            // changes with the page shown, so the selection restarts
  uint32_t list_try_ms;        // when the last profile fetch was started

  dk::Profile open;            // the profile whose slots are shown (a copy: paging does not disturb it)

  char key[40];                // Idempotency-Key of the key press being served
  char run_profile[dk::kIdCap];
  uint8_t run_slot;
  char run_label[dk::kLabelCap];
  Kind kind;
  char err[200];
  bool from_history;
  dk::Job job;
  bool job_known;              // job holds a reply of the backend (not only the id taken from History)
  Act act;
  uint32_t confirm_until_ms;
  uint32_t shown_sec;
  bool confirm_sent;
  uint32_t next_poll_ms;
  uint16_t polls;
  uint8_t poll_failures;

  dk::HistoryPage hist;
  Load hist_load;
  char hist_error[32];
  uint8_t hist_serial;
};

State s;
char s_empty[32];  // text of an empty list (read by the UI after view() returns)

struct Work {  // heap, per request: the reply and what it was parsed into
  char resp[kRespCap];
  union {
    dk::ProfilePage profiles;
    dk::Job job;
    dk::HistoryPage hist;
  } out;
  char path[112];
  char body[200];
};

// ---- Claude Code row: state (about 1.7 KiB) and the worker's scratch -----------------------------------------

enum class CRes : uint8_t { None, Sending, Shown };  // what the ClaudeResult page shows

struct ClaudeState {
  cl::ApprovalPage list;                    // the last answer; an item is dropped when its window closes
  uint32_t deadline_ms[cl::kMaxApprovals];  // local end of each item's window
  uint32_t list_sig;                        // hash of the ids shown: changes when the set of requests changes
  Load list_load;
  bool have_list;
  int list_code;                            // HTTP status or transport code of the last failed fetch
  uint8_t list_serial;
  uint8_t list_gen;                         // follows list_sig: the selection restarts when the requests change
  uint32_t list_try_ms;

  cl::Status status;
  Load status_load;
  bool have_status;
  int status_code;
  uint8_t status_serial;
  uint32_t status_try_ms;
  uint32_t status_got_ms;                   // when the reply arrived: its age is counted from here
  Page status_from;

  cl::Approval cur;                         // the request on the confirm page: a copy, refreshes do not move it
  uint32_t cur_deadline_ms;
  bool answered;                            // an answer for `cur` was started
  uint32_t shown_sec;
  char act_id[cl::kIdCap];                  // what the decide request is about, fixed when the key is pressed
  char act_nonce[cl::kNonceCap];
  bool act_allow;
  uint8_t flow;                             // generation of the answer in flight; older replies are dropped
  CRes res;
  char res_title[32];
  char res_body[200];
};

ClaudeState cs;

// Worker task only (one request at a time): a static buffer instead of a heap block per refresh.
struct ClaudeWork {
  char resp[kClaudeResp];
  union {
    cl::ApprovalPage page;
    cl::Status status;
    cl::DecideReply reply;
  } out;
};
ClaudeWork s_cw;

// ---- small helpers ---------------------------------------------------------------------------------

void copy_text(char* dst, size_t n, const char* src) { std::snprintf(dst, n, "%s", src != nullptr ? src : ""); }

void wipe(void* p, size_t n) {
  volatile uint8_t* v = static_cast<volatile uint8_t*>(p);
  while (n-- > 0) *v++ = 0;
}

bool due(uint32_t now, uint32_t at) { return static_cast<int32_t>(now - at) >= 0; }

int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

// "TT" + payload: the two hex digits are the generation tag of the request.
bool split_arg(const char* arg, uint8_t* tag, const char** rest) {
  const int hi = arg != nullptr ? hex_value(arg[0]) : -1;
  const int lo = hi >= 0 ? hex_value(arg[1]) : -1;
  if (lo < 0) return false;
  *tag = static_cast<uint8_t>(hi * 16 + lo);
  *rest = arg + 2;
  return true;
}

// Sends one request to the worker. Guard held. False when the worker queue is full.
bool enqueue(Call c, uint8_t tag, const char* payload) {
  char arg[48];
  std::snprintf(arg, sizeof(arg), "%02x%s", static_cast<unsigned>(tag), payload != nullptr ? payload : "");
  if (!request(Id::Deck, c, arg)) return false;
  ++s.inflight;
  s.inflight_since = millis();
  return true;
}

void call_done() {
  Guard g;
  if (s.inflight > 0) --s.inflight;
  changed();
}

const dk::Slot* open_slot(uint8_t n) { return dk::find_slot(s.open, n); }

// The label of the slot a job belongs to, when it is the profile that is open.
const char* label_for(const dk::Job& j) {
  if (std::strcmp(j.profile_id, s.open.id) == 0) {
    const dk::Slot* sl = open_slot(j.slot);
    if (sl != nullptr) return sl->label;
  }
  return "";
}

// Short list-level error (one row, <= 28 characters).
void short_error(int status, char* out, size_t n) {
  const char* t;
  if (status == -1) t = "Backend offline. r:Retry";
  else if (status == -6) t = "Clock not set. r:Retry";
  else if (status < 0) t = "No answer. r:Retry";
  else if (status == 401 || status == 403) t = "Device not accepted";
  else if (status == 404) t = "Not found. r:Retry";
  else if (status == 429) t = "Rate limited. r:Retry";
  else if (status == 503) t = "Backend busy. r:Retry";
  else {
    std::snprintf(out, n, "Backend error %d", status);
    return;
  }
  copy_text(out, n, t);
}

// ---- Claude Code row: helpers (Guard held) ---------------------------------------------------------

uint32_t claude_signature() {  // FNV-1a over the ids, in order
  uint32_t h = 2166136261u;
  for (uint8_t i = 0; i < cs.list.count; ++i) {
    for (const char* p = cs.list.items[i].id; *p != '\0'; ++p) h = (h ^ static_cast<uint8_t>(*p)) * 16777619u;
    h = (h ^ 0xFFu) * 16777619u;
  }
  return h;
}

// The set of requests may have changed: the selection restarts, so Enter never lands on a request that moved.
void claude_list_changed() {
  const uint32_t sig = claude_signature();
  if (sig != cs.list_sig) {
    cs.list_sig = sig;
    ++cs.list_gen;
  }
}

void claude_drop_item(uint8_t i) {
  if (i >= cs.list.count) return;
  for (uint8_t k = i; k + 1 < cs.list.count; ++k) {
    cs.list.items[k] = cs.list.items[k + 1];
    cs.deadline_ms[k] = cs.deadline_ms[k + 1];
  }
  const uint8_t last = static_cast<uint8_t>(cs.list.count - 1);
  wipe(&cs.list.items[last], sizeof(cs.list.items[last]));  // carried a nonce
  cs.deadline_ms[last] = 0;
  --cs.list.count;
}

void claude_drop_id(const char* id) {
  for (uint8_t i = 0; i < cs.list.count; ++i) {
    if (std::strcmp(cs.list.items[i].id, id) == 0) {
      claude_drop_item(i);
      claude_list_changed();
      return;
    }
  }
}

// Drops the requests whose window has closed. True when one went.
bool claude_expire_items(uint32_t now) {
  bool any = false;
  for (uint8_t i = 0; i < cs.list.count;) {
    if (cl::deadline_passed(now, cs.deadline_ms[i])) {
      claude_drop_item(i);
      any = true;
    } else {
      ++i;
    }
  }
  if (any) claude_list_changed();
  return any;
}

// The row is offered once the profile list or the approvals list has been read at least once, so a device
// without any profile still reaches it.
bool claude_row_visible() { return s.have_profiles || cs.have_list; }

cl::EntryState claude_entry_state() {
  if (cs.have_list) return cl::EntryState::Pending;
  if (cs.list_load == Load::Failed) return cl::entry_state_for_failure(cs.list_code);
  return cl::EntryState::Checking;
}

void claude_start_list() {
  if (cs.list_load == Load::Loading) return;
  ++cs.list_serial;
  cs.list_load = Load::Loading;
  cs.list_try_ms = millis();
  if (!enqueue(kCallClaudeList, cs.list_serial, nullptr)) {
    cs.list_load = Load::Failed;
    cs.list_code = kHttpRadioBusy;
  }
}

void claude_start_status() {
  if (cs.status_load == Load::Loading) return;
  ++cs.status_serial;
  cs.status_load = Load::Loading;
  cs.status_try_ms = millis();
  if (!enqueue(kCallClaudeStatus, cs.status_serial, nullptr)) {
    cs.status_load = Load::Failed;
    cs.status_code = kHttpRadioBusy;
  }
}

// Entering the module: nothing of an earlier visit is kept (no nonce, no old request); replies still on their
// way are dropped by their tags.
void claude_reset() {
  const uint8_t list_serial = static_cast<uint8_t>(cs.list_serial + 1);
  const uint8_t status_serial = static_cast<uint8_t>(cs.status_serial + 1);
  const uint8_t flow = static_cast<uint8_t>(cs.flow + 1);
  const uint8_t gen = static_cast<uint8_t>(cs.list_gen + 1);
  wipe(&cs, sizeof(cs));
  cs.list_serial = list_serial;
  cs.status_serial = status_serial;
  cs.flow = flow;
  cs.list_gen = gen;
  cs.status_from = Page::Profiles;
}

void claude_open_list() {
  s.page = Page::ClaudeList;
  cs.shown_sec = 0xFFFFFFFFu;
  claude_start_list();
  if (!cs.have_status || due(millis(), cs.status_try_ms + kClaudeStatusMs)) claude_start_status();
}

void claude_open_status(Page from) {
  cs.status_from = from;
  s.page = Page::ClaudeStatus;
  claude_start_status();
}

void claude_open_confirm(uint8_t i) {
  if (i >= cs.list.count) return;
  if (cl::deadline_passed(millis(), cs.deadline_ms[i])) {
    claude_expire_items(millis());
    toast("Expired");
    return;
  }
  std::memcpy(&cs.cur, &cs.list.items[i], sizeof(cs.cur));
  cs.cur_deadline_ms = cs.deadline_ms[i];
  cs.answered = false;
  cs.shown_sec = 0xFFFFFFFFu;
  wipe(cs.act_nonce, sizeof(cs.act_nonce));
  s.page = Page::ClaudeConfirm;
}

// Enter (allow) or Back / emergency cancel (deny) on the confirm page. The answer is sent once and never
// retried; the nonce goes into the request and is wiped here and in the worker.
void claude_answer(bool allow) {
  if (s.page != Page::ClaudeConfirm || cs.answered) return;
  cs.answered = true;
  copy_text(cs.act_id, sizeof(cs.act_id), cs.cur.id);
  copy_text(cs.act_nonce, sizeof(cs.act_nonce), cs.cur.nonce);
  wipe(cs.cur.nonce, sizeof(cs.cur.nonce));
  cs.act_allow = allow;
  ++cs.flow;
  cs.res = CRes::Sending;
  s.page = Page::ClaudeResult;
  if (!enqueue(kCallClaudeDecide, cs.flow, allow ? "a" : "d")) {
    wipe(cs.act_nonce, sizeof(cs.act_nonce));
    dk::ApiError none;
    std::memset(&none, 0, sizeof(none));
    cl::format_failure(cl::FailureKind::NotSent, cl::kStatusBuildFailed, none, cs.cur.tool, cs.res_title,
                       sizeof(cs.res_title), cs.res_body, sizeof(cs.res_body));
    cs.res = CRes::Shown;
  }
}

// The window of the open request closed before it was answered: nothing is sent.
void claude_expire_confirm() {
  cs.answered = true;
  wipe(cs.cur.nonce, sizeof(cs.cur.nonce));
  claude_drop_id(cs.cur.id);
  cl::format_expired(cs.cur.tool, cs.res_title, sizeof(cs.res_title), cs.res_body, sizeof(cs.res_body));
  cs.res = CRes::Shown;
  s.page = Page::ClaudeResult;
}

// ---- list fetches (Guard held) ---------------------------------------------------------------------

void start_list_fetch(uint8_t idx) {
  if (idx >= kMaxListPages) return;
  ++s.list_serial;
  s.list = Load::Loading;
  s.list_try_ms = millis();
  char payload[dk::kIdCap + 2];
  std::snprintf(payload, sizeof(payload), "%u%s", static_cast<unsigned>(idx), s.cursors[idx]);  // page index, cursor
  if (!enqueue(kCallProfiles, s.list_serial, payload)) {
    s.list = Load::Failed;
    s.list_offline = false;
    copy_text(s.list_error, sizeof(s.list_error), "Device busy. r:Retry");
  }
}

void start_hist_fetch() {
  ++s.hist_serial;
  s.hist_load = Load::Loading;
  if (!enqueue(kCallHistory, s.hist_serial, nullptr)) {
    s.hist_load = Load::Failed;
    copy_text(s.hist_error, sizeof(s.hist_error), "Device busy. r:Retry");
  }
}

// ---- the run / confirm / result flow (Guard held) ---------------------------------------------------

void new_flow(Page p) {
  ++s.token;
  s.page = p;
}

void set_error(const char* text, Kind kind = Kind::Error) {
  copy_text(s.err, sizeof(s.err), text);
  s.kind = kind;
  s.page = Page::Result;
}

void start_poll_soon(uint32_t delay_ms) {
  s.next_poll_ms = millis() + delay_ms;
  s.polls = 0;
  s.poll_failures = 0;
}

// A job reply was accepted: a confirmation is asked for, or the result page follows the job.
void adopt_job(const dk::Job& job) {
  std::memcpy(&s.job, &job, sizeof(s.job));
  s.job_known = true;
  if (job.state == dk::JobState::AwaitingConfirmation && job.requires_confirmation && job.nonce[0] != '\0') {
    const uint32_t window_ms = (job.confirm_window_s > 0 ? job.confirm_window_s : 120u) * 1000u;
    s.confirm_until_ms = millis() + (window_ms > 6000u ? window_ms - 3000u : 3000u);  // the job is older than this reply
    s.confirm_sent = false;
    s.shown_sec = 0xFFFFFFFFu;
    s.page = Page::Confirm;
    return;
  }
  s.kind = Kind::Job;
  s.page = Page::Result;
  start_poll_soon(600);
}

void start_run(uint8_t slot) {
  if (s.page != Page::Slots) return;
  const dk::Slot* sl = open_slot(slot);
  if (sl == nullptr) {
    toast("That slot is empty");
    return;
  }
  dk::format_idempotency_key(s.key, sizeof(s.key), esp_random(), esp_random(), millis());  // one per key press
  copy_text(s.run_profile, sizeof(s.run_profile), s.open.id);
  copy_text(s.run_label, sizeof(s.run_label), sl->label);
  s.run_slot = slot;
  s.from_history = false;
  s.job_known = false;
  std::memset(&s.job, 0, sizeof(s.job));
  new_flow(Page::Working);
  if (!enqueue(kCallRun, s.token, nullptr)) {
    set_error("The device is busy. Nothing was sent. Try again.");
  }
}

// Same key press again after a lost answer: the backend returns the job it already has, or creates it once.
void resend_run() {
  new_flow(Page::Working);
  if (!enqueue(kCallRun, s.token, nullptr)) set_error("The device is busy. Try again.", Kind::RunUnknown);
}

void send_cancel(const char* job_id) {
  copy_text(s.act.id, sizeof(s.act.id), job_id);
  wipe(s.act.nonce, sizeof(s.act.nonce));
  if (!enqueue(kCallCancel, s.token, nullptr)) {
    set_error("The device is busy. Nothing runs without your approval; the request expires by itself.");
  }
}

// Rejecting a confirmation (Back, emergency cancel or the window running out).
void reject_confirmation() {
  if (s.page != Page::Confirm || s.confirm_sent) return;
  s.confirm_sent = true;
  char id[dk::kTokenCap];
  copy_text(id, sizeof(id), s.job.id);
  wipe(s.job.nonce, sizeof(s.job.nonce));
  new_flow(Page::Result);
  s.kind = Kind::Cancelling;
  send_cancel(id);
}

void approve_confirmation() {
  if (s.page != Page::Confirm || s.confirm_sent) return;
  s.confirm_sent = true;
  copy_text(s.act.id, sizeof(s.act.id), s.job.id);
  copy_text(s.act.nonce, sizeof(s.act.nonce), s.job.nonce);
  wipe(s.job.nonce, sizeof(s.job.nonce));
  new_flow(Page::Result);
  s.kind = Kind::Approving;
  if (!enqueue(kCallConfirm, s.token, nullptr)) {
    wipe(s.act.nonce, sizeof(s.act.nonce));
    set_error("The device is busy. The approval was NOT sent. Nothing ran.");
  }
}

void send_poll() {
  s.next_poll_ms = millis() + kPollMs;
  if (s.job.id[0] == '\0' || !enqueue(kCallPoll, s.token, nullptr)) return;  // tried again at the next tick
}

void leave_result() {
  wipe(s.act.nonce, sizeof(s.act.nonce));
  ++s.token;
  s.page = s.from_history ? Page::History : (s.open.id[0] != '\0' ? Page::Slots : Page::Profiles);
}

// A request got no answer within kNetTimeoutMs: what the page shows must not stay "working" for ever.
void give_up_waiting() {
  s.inflight = 0;
  ++s.token;  // a late answer is dropped
  ++s.list_serial;
  ++s.hist_serial;
  if (s.list == Load::Loading) {
    s.list = Load::Failed;
    s.list_offline = true;
    copy_text(s.list_error, sizeof(s.list_error), "No answer. r:Retry");
  }
  if (s.hist_load == Load::Loading) {
    s.hist_load = Load::Failed;
    copy_text(s.hist_error, sizeof(s.hist_error), "No answer. r:Retry");
  }
  ++cs.list_serial;
  ++cs.status_serial;
  if (cs.list_load == Load::Loading) {
    cs.list_load = Load::Failed;
    cs.list_code = -3;
  }
  if (cs.status_load == Load::Loading) {
    cs.status_load = Load::Failed;
    cs.status_code = -3;
  }
  if (s.page == Page::ClaudeResult && cs.res == CRes::Sending) {
    ++cs.flow;  // a late reply is dropped; the answer is NOT sent again
    dk::ApiError none;
    std::memset(&none, 0, sizeof(none));
    cl::format_failure(cl::FailureKind::Unknown, -3, none, cs.cur.tool, cs.res_title, sizeof(cs.res_title),
                       cs.res_body, sizeof(cs.res_body));
    cs.res = CRes::Shown;
  }
  switch (s.page) {
    case Page::Working:
      set_error("No answer from the backend. The action may or may not have started. Enter checks again with the "
                "same key; it never runs twice.",
                Kind::RunUnknown);
      break;
    case Page::Result:
      if (s.kind == Kind::Approving) {
        set_error("The approval got no answer. The action may or may not have started. Enter checks the job.",
                  Kind::CheckLater);
      } else if (s.kind == Kind::Cancelling) {
        set_error("No answer to the rejection. Nothing runs without your approval; the request expires by itself.");
      } else if (s.kind == Kind::Loading || (s.kind == Kind::Job && !dk::job_is_terminal(s.job.state))) {
        set_error("No answer from the backend. The job may still be running. Enter asks again.", Kind::CheckLater);
      }
      break;
    default:
      break;
  }
}

// ---- worker side: HTTPS + parsing; results are applied under the Guard ---------------------------------

Work* alloc_work() { return new (std::nothrow) Work; }

int send_request(Work& w, const char* method, const char* path, const char* body, bool idempotent, const char* idem_key,
                 size_t* n) {
  *n = 0;
  return http(method, path, body, body != nullptr ? std::strlen(body) : 0, w.resp, sizeof(w.resp), n, idempotent,
              idem_key);
}

constexpr int kNoMemory = -7;

void error_text(int status, const Work* w, size_t n, char* out, size_t cap) {
  dk::ApiError e;
  if (w != nullptr && status > 0) {
    dk::parse_api_error(w->resp, n, e);
  } else {
    std::memset(&e, 0, sizeof(e));
  }
  if (status == kNoMemory) {
    copy_text(out, cap, "Out of memory on the device. Nothing was sent.");
    return;
  }
  dk::format_error(status, e, out, cap);
}

void do_profiles(const char* arg) {
  uint8_t serial = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &serial, &rest) || rest[0] < '0' || rest[0] >= '0' + static_cast<char>(kMaxListPages)) return;
  const uint8_t idx = static_cast<uint8_t>(rest[0] - '0');
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) {
    st = dk::path_profiles(w->path, sizeof(w->path), rest + 1) ? send_request(*w, "GET", w->path, nullptr, true, nullptr, &n) : -4;
  }
  const bool parsed = st == 200 && dk::parse_profiles(w->resp, n, w->out.profiles);
  {
    Guard g;
    if (serial == s.list_serial) {
      if (parsed) {
        if (idx != s.page_idx) ++s.list_gen;
        std::memcpy(&s.profiles, &w->out.profiles, sizeof(s.profiles));
        s.have_profiles = true;
        s.list = Load::Ready;
        s.list_offline = false;
        s.list_error[0] = '\0';
        s.page_idx = idx;
        if (idx + 1 < kMaxListPages) copy_text(s.cursors[idx + 1], sizeof(s.cursors[idx + 1]), w->out.profiles.next_after);
      } else {
        s.list = Load::Failed;
        s.list_offline = st < 0;
        short_error(st == 200 ? -4 : st, s.list_error, sizeof(s.list_error));
        if (s.have_profiles) toast(s.list_error);
      }
    }
  }
  delete w;
}

void do_history(const char* arg) {
  uint8_t serial = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &serial, &rest)) return;
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) {
    st = dk::path_history(w->path, sizeof(w->path), dk::kMaxHistory) ? send_request(*w, "GET", w->path, nullptr, true, nullptr, &n) : -4;
  }
  const bool parsed = st == 200 && dk::parse_history(w->resp, n, w->out.hist);
  {
    Guard g;
    if (serial == s.hist_serial) {
      if (parsed) {
        std::memcpy(&s.hist, &w->out.hist, sizeof(s.hist));
        s.hist_load = Load::Ready;
      } else {
        s.hist_load = Load::Failed;
        short_error(st == 200 ? -4 : st, s.hist_error, sizeof(s.hist_error));
      }
    }
  }
  delete w;
}

void do_run(const char* arg) {
  uint8_t tag = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &tag, &rest)) return;
  char profile[dk::kIdCap];
  char idem[sizeof(s.key)];
  uint8_t slot;
  {
    Guard g;
    copy_text(profile, sizeof(profile), s.run_profile);
    copy_text(idem, sizeof(idem), s.key);
    slot = s.run_slot;
  }
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) {
    st = dk::path_run(w->path, sizeof(w->path), profile, slot)
             ? send_request(*w, "POST", w->path, nullptr, false, idem, &n)  // never resent by the client
             : -4;
  }
  const bool parsed = st == 200 && dk::parse_job(w->resp, n, w->out.job);
  {
    Guard g;
    if (tag == s.token) {
      if (parsed) {
        adopt_job(w->out.job);
      } else if (st == 200 || dk::status_may_have_reached_backend(st)) {
        set_error("No usable answer from the backend. The action may or may not have started. Enter checks again "
                  "with the same key; it never runs twice.",
                  Kind::RunUnknown);
      } else {
        char text[sizeof(s.err)];
        error_text(st, w, n, text, sizeof(text));
        set_error(text);
      }
    }
  }
  delete w;
}

void do_confirm(const char* arg) {
  uint8_t tag = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &tag, &rest)) return;
  Act act;
  {
    Guard g;
    std::memcpy(&act, &s.act, sizeof(act));
    wipe(s.act.nonce, sizeof(s.act.nonce));
  }
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) {
    st = dk::path_confirm(w->path, sizeof(w->path), act.id) && dk::body_confirm(w->body, sizeof(w->body), act.id, act.nonce, true)
             ? send_request(*w, "POST", w->path, w->body, false, nullptr, &n)  // an approval is sent once
             : -4;
    wipe(w->body, sizeof(w->body));
  }
  wipe(act.nonce, sizeof(act.nonce));
  const bool parsed = st == 200 && dk::parse_job(w->resp, n, w->out.job);
  {
    Guard g;
    if (tag == s.token) {
      if (parsed) {
        std::memcpy(&s.job, &w->out.job, sizeof(s.job));
        s.job_known = true;
        s.kind = Kind::Job;
        start_poll_soon(600);
      } else if (st == 200 || dk::status_may_have_reached_backend(st)) {
        set_error("The approval got no usable answer. The action may or may not have started. Enter checks the job.",
                  Kind::CheckLater);
      } else {
        char text[sizeof(s.err)];
        error_text(st, w, n, text, sizeof(text));
        set_error(text);
      }
    }
  }
  delete w;
}

void do_cancel(const char* arg) {
  uint8_t tag = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &tag, &rest)) return;
  char id[dk::kTokenCap];
  {
    Guard g;
    copy_text(id, sizeof(id), s.act.id);
  }
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) st = dk::path_cancel(w->path, sizeof(w->path), id) ? send_request(*w, "POST", w->path, nullptr, true, nullptr, &n) : -4;
  const bool parsed = st == 200 && dk::parse_job(w->resp, n, w->out.job);
  {
    Guard g;
    if (tag == s.token) {
      if (parsed) {
        std::memcpy(&s.job, &w->out.job, sizeof(s.job));
        s.job_known = true;
        s.kind = Kind::Job;
      } else if (st > 0 && st != 200) {
        char text[sizeof(s.err)];
        error_text(st, w, n, text, sizeof(text));
        set_error(text);
      } else {
        set_error("The backend could not be told. Nothing runs without your approval; the request expires by itself.");
      }
    }
  }
  delete w;
}

void do_poll(const char* arg) {
  uint8_t tag = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &tag, &rest)) return;
  char id[dk::kTokenCap];
  {
    Guard g;
    copy_text(id, sizeof(id), s.job.id);
  }
  Work* w = alloc_work();
  int st = kNoMemory;
  size_t n = 0;
  if (w != nullptr) st = dk::path_job(w->path, sizeof(w->path), id) ? send_request(*w, "GET", w->path, nullptr, true, nullptr, &n) : -4;
  const bool parsed = st == 200 && dk::parse_job(w->resp, n, w->out.job) && std::strcmp(w->out.job.id, id) == 0;
  {
    Guard g;
    if (tag == s.token && s.page == Page::Result && std::strcmp(s.job.id, id) == 0) {
      if (parsed) {
        std::memcpy(&s.job, &w->out.job, sizeof(s.job));
        s.job_known = true;
        s.kind = Kind::Job;
        s.poll_failures = 0;
        if (!dk::job_is_terminal(s.job.state)) {
          s.next_poll_ms = millis() + kPollMs;
          if (++s.polls > kMaxPolls) {
            set_error("Still not finished after waiting a while. Enter asks the backend again.", Kind::CheckLater);
          }
        }
      } else if (st > 0 && st != 200 && st < 500 && st != 429) {
        char text[sizeof(s.err)];
        error_text(st, w, n, text, sizeof(text));
        set_error(text);  // 404 and friends: asking again will not help
      } else if (++s.poll_failures >= kMaxPollFailures) {
        set_error("Lost contact with the backend. The job may still be running. Enter asks again.", Kind::CheckLater);
      } else {
        s.next_poll_ms = millis() + kPollMs;
      }
    }
  }
  delete w;
}

// ---- worker side, Claude Code row ---------------------------------------------------------------------------
// One static scratch (s_cw) serves all three calls: the worker runs one request at a time. What it held (the
// reply carries nonces) is wiped before the call returns.

void do_claude_list(const char* arg) {
  uint8_t serial = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &serial, &rest)) return;
  ClaudeWork& w = s_cw;
  const uint32_t sent_ms = millis();  // the windows are counted from before the request: never longer than real
  size_t n = 0;
  const int st = http("GET", cl::kPathApprovals, nullptr, 0, w.resp, sizeof(w.resp), &n, true, nullptr);
  const bool parsed = st == 200 && cl::parse_approvals(w.resp, n, w.out.page);
  {
    Guard g;
    if (serial == cs.list_serial) {
      if (parsed) {
        std::memcpy(&cs.list, &w.out.page, sizeof(cs.list));
        for (uint8_t i = 0; i < cs.list.count; ++i) {
          cs.deadline_ms[i] = cl::deadline_from(sent_ms, cs.list.items[i].expires_in_s);
        }
        cs.have_list = true;
        cs.list_load = Load::Ready;
        cs.list_code = 0;
        claude_expire_items(millis());
        claude_list_changed();
      } else {
        cs.list_load = Load::Failed;
        cs.list_code = st == 200 ? -4 : st;  // an unreadable 200 is a protocol problem
      }
    }
  }
  wipe(w.resp, sizeof(w.resp));
  wipe(&w.out, sizeof(w.out));
}

void do_claude_status(const char* arg) {
  uint8_t serial = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &serial, &rest)) return;
  ClaudeWork& w = s_cw;
  size_t n = 0;
  const int st = http("GET", cl::kPathStatus, nullptr, 0, w.resp, sizeof(w.resp), &n, true, nullptr);
  const bool parsed = st == 200 && cl::parse_status(w.resp, n, w.out.status);
  {
    Guard g;
    if (serial == cs.status_serial) {
      if (parsed) {
        std::memcpy(&cs.status, &w.out.status, sizeof(cs.status));
        cs.have_status = true;
        cs.status_load = Load::Ready;
        cs.status_code = 0;
        cs.status_got_ms = millis();
      } else {
        cs.status_load = Load::Failed;
        cs.status_code = st == 200 ? -4 : st;
      }
    }
  }
  wipe(w.resp, sizeof(w.resp));
  wipe(&w.out, sizeof(w.out));
}

// The answer to one request, sent once: never retried by the client (idempotent=false) and never again by the
// module, whatever comes back.
void do_claude_decide(const char* arg) {
  uint8_t tag = 0;
  const char* rest = nullptr;
  if (!split_arg(arg, &tag, &rest)) return;
  const bool allow = rest[0] == 'a';
  char id[cl::kIdCap];
  char nonce[cl::kNonceCap];
  char tool[cl::kToolCap];
  {
    Guard g;
    copy_text(id, sizeof(id), cs.act_id);
    copy_text(nonce, sizeof(nonce), cs.act_nonce);
    copy_text(tool, sizeof(tool), cs.cur.tool);
    wipe(cs.act_nonce, sizeof(cs.act_nonce));
  }
  ClaudeWork& w = s_cw;
  char path[96];
  char body[96];
  int st = cl::kStatusBuildFailed;  // nothing is sent when the path or body cannot be built
  size_t n = 0;
  if (cl::path_decide(path, sizeof(path), id) && cl::body_decide(body, sizeof(body), allow, nonce)) {
    st = http("POST", path, body, std::strlen(body), w.resp, sizeof(w.resp), &n, false, nullptr);
  }
  wipe(body, sizeof(body));
  wipe(nonce, sizeof(nonce));

  const bool parsed = st == 200 && cl::parse_decide_reply(w.resp, n, w.out.reply) &&
                      (w.out.reply.approval_id[0] == '\0' || std::strcmp(w.out.reply.approval_id, id) == 0);
  dk::ApiError err;
  std::memset(&err, 0, sizeof(err));
  if (st > 0 && st != 200) dk::parse_api_error(w.resp, n, err);
  char title[sizeof(cs.res_title)];
  char text[sizeof(cs.res_body)];
  cl::FailureKind kind = cl::FailureKind::Unknown;
  if (parsed) {
    cl::format_answer_ok(w.out.reply.state == cl::Decided::Allowed, tool, title, sizeof(title), text, sizeof(text));
  } else {
    kind = cl::classify_failure(st, err);
    cl::format_failure(kind, st, err, tool, title, sizeof(title), text, sizeof(text));
  }
  // The request is no longer pending when the backend recorded the answer or said it is gone / closed.
  const bool settled = parsed || kind == cl::FailureKind::TooLate || kind == cl::FailureKind::AlreadyAnswered ||
                       kind == cl::FailureKind::Gone;
  {
    Guard g;
    if (settled) claude_drop_id(id);
    if (tag == cs.flow && cs.res == CRes::Sending) {
      copy_text(cs.res_title, sizeof(cs.res_title), title);
      copy_text(cs.res_body, sizeof(cs.res_body), text);
      cs.res = CRes::Shown;
    } else {
      char note[40];  // the page was left meanwhile: say how it ended
      std::snprintf(note, sizeof(note), "Claude Code: %s", title[0] == '!' ? title + 1 : title);
      toast(note);
    }
  }
  wipe(w.resp, sizeof(w.resp));
  wipe(&w.out, sizeof(w.out));
}

void call(uint8_t call_id, const char* arg) {
  switch (call_id) {
    case kCallProfiles: do_profiles(arg); break;
    case kCallRun: do_run(arg); break;
    case kCallConfirm: do_confirm(arg); break;
    case kCallCancel: do_cancel(arg); break;
    case kCallPoll: do_poll(arg); break;
    case kCallHistory: do_history(arg); break;
    case kCallClaudeList: do_claude_list(arg); break;
    case kCallClaudeStatus: do_claude_status(arg); break;
    case kCallClaudeDecide: do_claude_decide(arg); break;
    default: break;
  }
  call_done();  // exactly once per request: clears "working" and redraws
}

// ---- UI side ------------------------------------------------------------------------------------------

bool available(const char** why) {
  (void)why;
  return true;  // an unreachable backend is explained on the page itself
}

enum class RowKind : uint8_t { Profile, Claude, More, Prev, History, None };

bool has_more() { return s.profiles.next_after[0] != '\0' && s.page_idx + 1 < kMaxListPages; }

// Rows of the first page: the profiles, the Claude Code row, then "More profiles...", "Previous profiles" and
// "History" (those three only when there is a profile).
uint16_t profile_rows() {
  const uint16_t n = s.have_profiles ? s.profiles.count : 0;
  uint16_t rows = n;
  if (claude_row_visible()) ++rows;
  if (n > 0) rows = static_cast<uint16_t>(rows + (has_more() ? 1 : 0) + (s.page_idx > 0 ? 1 : 0) + 1);
  return rows;
}

RowKind row_kind(uint16_t i, uint16_t* index) {
  const uint16_t n = s.have_profiles ? s.profiles.count : 0;
  if (i < n) {
    *index = i;
    return RowKind::Profile;
  }
  i = static_cast<uint16_t>(i - n);
  if (claude_row_visible()) {
    if (i == 0) return RowKind::Claude;
    --i;
  }
  if (n == 0) return RowKind::None;
  if (has_more()) {
    if (i == 0) return RowKind::More;
    --i;
  }
  if (s.page_idx > 0) {
    if (i == 0) return RowKind::Prev;
    --i;
  }
  return i == 0 ? RowKind::History : RowKind::None;
}

void row_profiles(uint16_t i, char* out, size_t n) {
  Guard g;
  uint16_t index = 0;
  switch (row_kind(i, &index)) {
    case RowKind::Profile: dk::format_profile_row(s.profiles.profiles[index], out, n); break;
    case RowKind::Claude: cl::format_entry_row(claude_entry_state(), cs.list.count, out, n); break;
    case RowKind::More: copy_text(out, n, "More profiles..."); break;
    case RowKind::Prev: copy_text(out, n, "Previous profiles"); break;
    case RowKind::History: copy_text(out, n, "History"); break;
    case RowKind::None: copy_text(out, n, ""); break;
  }
}

void row_slots(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i < s.open.slot_count) {
    dk::format_slot_row(s.open.slots[i], out, n);
  } else {
    copy_text(out, n, "");
  }
}

void row_history(uint16_t i, char* out, size_t n) {
  Guard g;
  if (i < s.hist.count) {
    dk::format_history_row(s.hist.entries[i], out, n);
  } else {
    copy_text(out, n, "");
  }
}

void strip(PageView& v, const char* keys) {
  copy_text(v.left, sizeof(v.left), s.inflight > 0 ? "Working... Del:Back" : keys);
}

void view_profiles(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = static_cast<uint8_t>(static_cast<uint8_t>(Page::Profiles) | ((s.list_gen & 7u) << 4));
  copy_text(v.title, sizeof(v.title), "Control Deck");
  v.count = profile_rows();
  v.row = row_profiles;
  if (!s.have_profiles) {
    copy_text(s_empty, sizeof(s_empty), s.list == Load::Failed ? s.list_error : "Loading profiles...");
  } else {
    copy_text(s_empty, sizeof(s_empty), "No profile for this device");
  }
  v.empty = s_empty;
  strip(v, "Ent:Open r:Refresh Del:Back");
  if (s.list == Load::Failed && s.have_profiles) {
    copy_text(v.right, sizeof(v.right), "stale");
  } else if (s.page_idx > 0) {
    std::snprintf(v.right, sizeof(v.right), "pg %u", static_cast<unsigned>(s.page_idx) + 1);
  } else if (v.count > 0 && (!s.have_profiles || s.profiles.count == 0)) {
    // only the Claude Code row is offered: say why there is nothing else
    copy_text(v.right, sizeof(v.right), s.have_profiles ? "no profile" : "no profiles yet");
  }
}

// ---- Claude Code row: pages -----------------------------------------------------------------------------

void claude_usage_row(char* out, size_t n) {
  if (cs.have_status) {
    cl::format_usage_line(cs.status, out, n);
  } else if (cs.status_load == Load::Failed) {
    copy_text(out, n, "Usage: not available");
  } else {
    copy_text(out, n, "Usage: checking...");
  }
}

// Rows of the approvals page: the pending requests (or one note when there are none), then the usage line.
uint8_t claude_pending_count() { return cs.have_list ? cs.list.count : 0; }

uint16_t claude_list_rows() {
  const uint8_t n = claude_pending_count();
  return static_cast<uint16_t>((n == 0 ? 1 : n) + 1);
}

void row_claude(uint16_t i, char* out, size_t n) {
  Guard g;
  const uint8_t pending = claude_pending_count();
  if (pending == 0 && i == 0) {
    cl::format_list_note(claude_entry_state(), 0, out, n);
  } else if (i < pending) {
    const uint32_t left = cl::seconds_left(millis(), cs.deadline_ms[i]);
    cl::format_approval_row(cs.list.items[i], left, out, n);
  } else {
    claude_usage_row(out, n);
  }
}

void view_claude_list(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = static_cast<uint8_t>(static_cast<uint8_t>(Page::ClaudeList) | ((cs.list_gen & 7u) << 4));
  copy_text(v.title, sizeof(v.title), "Claude Code");
  v.count = claude_list_rows();
  v.row = row_claude;
  copy_text(v.left, sizeof(v.left), "Ent:Open s:Usage r:Refresh");
  if (cs.list_load == Load::Failed && cs.have_list) copy_text(v.right, sizeof(v.right), "stale");
}

void view_claude_confirm(PageView& v) {
  v.kind = PageKind::Confirm;
  v.page_id = static_cast<uint8_t>(Page::ClaudeConfirm);
  copy_text(v.title, sizeof(v.title), "CONFIRM ACTION");
  cl::format_confirm_operation(cs.cur.tool, v.confirm_operation, sizeof(v.confirm_operation));
  cl::format_confirm_target(v.confirm_target, sizeof(v.confirm_target));
  cl::format_confirm_effect(cs.cur.summary, v.confirm_effect, sizeof(v.confirm_effect));
  copy_text(v.left, sizeof(v.left), "Ent:Allow once Del:Deny");
  std::snprintf(v.right, sizeof(v.right), "%lus",
                static_cast<unsigned long>(cl::seconds_left(millis(), cs.cur_deadline_ms)));
}

void view_claude_result(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = static_cast<uint8_t>(Page::ClaudeResult);
  if (cs.res == CRes::Sending) {
    copy_text(v.title, sizeof(v.title), "Sending answer");
    std::snprintf(v.body, sizeof(v.body),
                  "Claude Code: %s\nSending your answer to the backend...\nNothing is assumed until it answers.",
                  cs.cur.tool[0] != '\0' ? cs.cur.tool : "?");
    copy_text(v.left, sizeof(v.left), "Working... Del:Back");
  } else {
    copy_text(v.title, sizeof(v.title), cs.res_title);
    copy_text(v.body, sizeof(v.body), cs.res_body);
    copy_text(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
  }
}

void view_claude_status(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = static_cast<uint8_t>(Page::ClaudeStatus);
  copy_text(v.title, sizeof(v.title), "Claude Code usage");
  copy_text(v.left, sizeof(v.left), "r:Refresh Del:Back");
  if (cs.have_status) {
    const uint32_t since_s = (millis() - cs.status_got_ms) / 1000u;
    cl::format_status_body(cs.status, cs.status.updated_s_ago + since_s, v.body, sizeof(v.body));
    if (cs.status_load == Load::Failed) {  // the numbers above are older than the last try
      char note[40];
      cl::format_list_note(cl::entry_state_for_failure(cs.status_code), 0, note, sizeof(note));
      const size_t len = std::strlen(v.body);
      std::snprintf(v.body + len, sizeof(v.body) - len, "\nRefresh failed: %s", note);
    }
  } else if (cs.status_load == Load::Failed) {
    char note[40];
    cl::format_list_note(cl::entry_state_for_failure(cs.status_code), 0, note, sizeof(note));
    std::snprintf(v.body, sizeof(v.body), "Could not read the usage line.\n%s", note);
  } else {
    copy_text(v.body, sizeof(v.body), "Asking the backend...");
  }
}

void view_slots(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = static_cast<uint8_t>(Page::Slots);
  copy_text(v.title, sizeof(v.title), s.open.name);
  v.count = s.open.slot_count;
  v.row = row_slots;
  copy_text(s_empty, sizeof(s_empty), "No slots in this profile");
  v.empty = s_empty;
  strip(v, "1-9:Run Ent:Run Del:Back");
}

void view_working(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = static_cast<uint8_t>(Page::Working);
  copy_text(v.title, sizeof(v.title), s.run_label);
  std::snprintf(v.body, sizeof(v.body), "Sending to the backend...\nNothing is assumed until it answers.");
  copy_text(v.left, sizeof(v.left), "Working... Del:Back");
}

void view_confirm(PageView& v) {
  v.kind = PageKind::Confirm;
  v.page_id = static_cast<uint8_t>(Page::Confirm);
  copy_text(v.title, sizeof(v.title), "CONFIRM ACTION");
  if (s.run_label[0] != '\0') {
    copy_text(v.confirm_operation, sizeof(v.confirm_operation), s.run_label);
  } else {
    std::snprintf(v.confirm_operation, sizeof(v.confirm_operation), "%s/%u", s.job.profile_id, static_cast<unsigned>(s.job.slot));
  }
  dk::format_confirm_target(s.job, v.confirm_target, sizeof(v.confirm_target));
  dk::format_confirm_effect(s.job, v.confirm_effect, sizeof(v.confirm_effect));
  const uint32_t now = millis();
  const uint32_t left_s = due(now, s.confirm_until_ms) ? 0 : (s.confirm_until_ms - now + 999) / 1000;
  copy_text(v.left, sizeof(v.left), "Ent:Approve Del:Reject");
  std::snprintf(v.right, sizeof(v.right), "%lus", static_cast<unsigned long>(left_s));
}

bool result_waits_for_enter() { return s.kind == Kind::RunUnknown || s.kind == Kind::CheckLater; }

bool result_is_polling() {
  return s.kind == Kind::Loading || (s.kind == Kind::Job && !dk::job_is_terminal(s.job.state));
}

void view_result(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = static_cast<uint8_t>(Page::Result);
  const char* label = s.job_known ? label_for(s.job) : s.run_label;
  switch (s.kind) {
    case Kind::Loading:
      copy_text(v.title, sizeof(v.title), "Loading job");
      std::snprintf(v.body, sizeof(v.body), "Asking the backend for the job...");
      break;
    case Kind::Approving:
      copy_text(v.title, sizeof(v.title), "Approved");
      std::snprintf(v.body, sizeof(v.body), "%s\nApproved on the device. Sending to the backend...", s.run_label);
      break;
    case Kind::Cancelling:
      copy_text(v.title, sizeof(v.title), "Rejecting");
      std::snprintf(v.body, sizeof(v.body), "%s\nRejecting on the backend. Nothing runs without your approval.", s.run_label);
      break;
    case Kind::Job:
      dk::format_job_title(s.job, v.title, sizeof(v.title));
      dk::format_job_body(s.job, label, v.body, sizeof(v.body));
      break;
    case Kind::Error:
    case Kind::RunUnknown:
    case Kind::CheckLater:
      copy_text(v.title, sizeof(v.title), s.kind == Kind::Error ? "!Not done" : "!Status unknown");
      if (s.run_label[0] != '\0') {
        std::snprintf(v.body, sizeof(v.body), "%s\n%s", s.run_label, s.err);
      } else {
        copy_text(v.body, sizeof(v.body), s.err);
      }
      break;
  }
  // The strip does not flicker to "Working..." for the periodic status polls.
  if (result_waits_for_enter()) {
    copy_text(v.left, sizeof(v.left), "!Ent:Check Del:Back");
  } else if (result_is_polling()) {
    copy_text(v.left, sizeof(v.left), "Waiting... Del:Back");
  } else {
    copy_text(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
  }
}

void view_history(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = static_cast<uint8_t>(Page::History);
  copy_text(v.title, sizeof(v.title), "History");
  v.count = s.hist_load == Load::Ready ? s.hist.count : 0;
  v.row = row_history;
  if (s.hist_load == Load::Failed) {
    copy_text(s_empty, sizeof(s_empty), s.hist_error);
  } else if (s.hist_load == Load::Ready) {
    copy_text(s_empty, sizeof(s_empty), "No finished jobs lately");
  } else {
    copy_text(s_empty, sizeof(s_empty), "Loading...");
  }
  v.empty = s_empty;
  strip(v, "Ent:Open r:Refresh Del:Back");
}

void view(PageView& v) {
  Guard g;
  switch (s.page) {
    case Page::Profiles: view_profiles(v); break;
    case Page::Slots: view_slots(v); break;
    case Page::Working: view_working(v); break;
    case Page::Confirm: view_confirm(v); break;
    case Page::Result: view_result(v); break;
    case Page::History: view_history(v); break;
    case Page::ClaudeList: view_claude_list(v); break;
    case Page::ClaudeConfirm: view_claude_confirm(v); break;
    case Page::ClaudeResult: view_claude_result(v); break;
    case Page::ClaudeStatus: view_claude_status(v); break;
  }
  // Periodic refreshes (job status, the approvals page) are not "working"; only an answer on its way is.
  const bool quiet = (s.page == Page::Result && s.kind == Kind::Job) || s.page == Page::ClaudeList ||
                     s.page == Page::ClaudeStatus || s.page == Page::ClaudeConfirm;
  v.busy = s.inflight > 0 && !quiet;
}

void open_history_job(uint16_t row) {
  if (row >= s.hist.count || s.hist_load != Load::Ready) return;
  const dk::HistoryEntry& e = s.hist.entries[row];
  if (e.job_id[0] == '\0') {
    toast("This entry has no job to open");
    return;
  }
  std::memset(&s.job, 0, sizeof(s.job));
  copy_text(s.job.id, sizeof(s.job.id), e.job_id);
  copy_text(s.job.profile_id, sizeof(s.job.profile_id), e.profile);
  s.job.slot = e.slot;
  s.job_known = false;
  s.from_history = true;
  s.run_label[0] = '\0';
  new_flow(Page::Result);
  s.kind = Kind::Loading;
  start_poll_soon(0);
  send_poll();
}

void select(uint16_t row) {
  Guard g;
  switch (s.page) {
    case Page::Profiles: {
      uint16_t index = 0;
      switch (row_kind(row, &index)) {
        case RowKind::Profile:
          std::memcpy(&s.open, &s.profiles.profiles[index], sizeof(s.open));
          s.page = Page::Slots;
          break;
        case RowKind::More: start_list_fetch(static_cast<uint8_t>(s.page_idx + 1)); break;
        case RowKind::Prev: start_list_fetch(static_cast<uint8_t>(s.page_idx - 1)); break;
        case RowKind::Claude: claude_open_list(); break;
        case RowKind::History:
          s.page = Page::History;
          start_hist_fetch();
          break;
        case RowKind::None: break;
      }
      break;
    }
    case Page::Slots:
      if (row < s.open.slot_count) start_run(s.open.slots[row].number);
      break;
    case Page::Working: break;
    case Page::Confirm: approve_confirmation(); break;
    case Page::Result:
      if (s.kind == Kind::RunUnknown) {
        resend_run();
      } else if (s.kind == Kind::CheckLater && s.job.id[0] != '\0') {
        s.kind = (s.job_known && !dk::job_is_terminal(s.job.state)) ? Kind::Job : Kind::Loading;
        start_poll_soon(0);
        send_poll();
      }
      break;
    case Page::History: open_history_job(row); break;
    case Page::ClaudeList: {
      const uint8_t pending = claude_pending_count();
      if (pending == 0) {
        if (row > 0) claude_open_status(Page::ClaudeList);  // row 0 is only the note "No requests pending"
      } else if (row < pending) {
        claude_open_confirm(static_cast<uint8_t>(row));
      } else {
        claude_open_status(Page::ClaudeList);
      }
      break;
    }
    case Page::ClaudeConfirm: claude_answer(true); break;
    case Page::ClaudeResult:
    case Page::ClaudeStatus: break;
  }
}

bool back() {
  Guard g;
  switch (s.page) {
    case Page::ClaudeList: s.page = Page::Profiles; return true;
    case Page::ClaudeStatus: s.page = cs.status_from; return true;
    case Page::ClaudeConfirm: claude_answer(false); return true;
    case Page::ClaudeResult:
      if (cs.res == CRes::Sending) {
        ++cs.flow;  // the reply is dropped; it is not sent again
        toast("Answer may still arrive: open the list");
      }
      s.page = Page::ClaudeList;
      cs.shown_sec = 0xFFFFFFFFu;
      claude_start_list();
      return true;
    case Page::Profiles: return false;  // leave the module
    case Page::Slots:
    case Page::History: s.page = Page::Profiles; return true;
    case Page::Working:
      new_flow(Page::Slots);
      toast("Stopped waiting. It may still run: see History.");
      return true;
    case Page::Confirm: reject_confirmation(); return true;
    case Page::Result: leave_result(); return true;
  }
  return false;
}

bool key(NavEvent e, char c) {
  if (e != NavEvent::Char) return false;
  Guard g;
  if (s.page == Page::Slots && c >= '1' && c <= '9') {
    start_run(static_cast<uint8_t>(c - '0'));
    return true;
  }
  if ((c == 's' || c == 'S') && ((s.page == Page::Profiles && claude_row_visible()) || s.page == Page::ClaudeList)) {
    claude_open_status(s.page);
    return true;
  }
  if (c == 'r' || c == 'R') {
    if (s.page == Page::Profiles) {
      start_list_fetch(s.page_idx);
      claude_start_list();  // the Claude Code row is read again with the profiles
      return true;
    }
    if (s.page == Page::History) {
      start_hist_fetch();
      return true;
    }
    if (s.page == Page::ClaudeList) {
      claude_start_list();
      claude_start_status();
      return true;
    }
    if (s.page == Page::ClaudeStatus) {
      claude_start_status();
      return true;
    }
  }
  return false;
}

bool tick(uint32_t now_ms) {
  Guard g;
  bool redraw = false;
  if (s.inflight > 0 && now_ms - s.inflight_since > kNetTimeoutMs) {
    give_up_waiting();
    redraw = true;
  }
  if (s.page == Page::Confirm && !s.confirm_sent) {
    if (due(now_ms, s.confirm_until_ms)) {
      reject_confirmation();  // the window ran out: the job is cancelled, never approved by default
      redraw = true;
    } else {
      const uint32_t sec = (s.confirm_until_ms - now_ms + 999) / 1000;
      if (sec != s.shown_sec) {
        s.shown_sec = sec;
        redraw = true;
      }
    }
  }
  if (s.page == Page::Result && result_is_polling() && s.inflight == 0 && due(now_ms, s.next_poll_ms)) send_poll();

  // Claude Code row. tick() is only called while this module is on screen, so nothing is polled from Home, from
  // Help or from another module. Requests whose window closed disappear here, without waiting for a refresh.
  if (cs.have_list && claude_expire_items(now_ms)) redraw = true;
  switch (s.page) {
    case Page::ClaudeList: {
      if (cs.list_load != Load::Loading && due(now_ms, cs.list_try_ms + kClaudeRefreshMs)) {
        claude_start_list();
      } else if (cs.status_load != Load::Loading && due(now_ms, cs.status_try_ms + kClaudeStatusMs)) {
        claude_start_status();
      }
      const uint32_t sec = now_ms / 1000u;  // the remaining seconds in the rows
      if (sec != cs.shown_sec) {
        cs.shown_sec = sec;
        redraw = true;
      }
      break;
    }
    case Page::ClaudeConfirm:
      if (!cs.answered) {
        if (cl::deadline_passed(now_ms, cs.cur_deadline_ms)) {
          claude_expire_confirm();  // nothing is sent: the window is closed
          redraw = true;
        } else {
          const uint32_t sec = cl::seconds_left(now_ms, cs.cur_deadline_ms);
          if (sec != cs.shown_sec) {
            cs.shown_sec = sec;
            redraw = true;
          }
        }
      }
      break;
    case Page::ClaudeStatus:
      if (cs.status_load != Load::Loading && due(now_ms, cs.status_try_ms + kClaudeStatusPageMs)) {
        claude_start_status();
      }
      break;
    default:
      break;
  }
  return redraw;
}

void cancel() {
  Guard g;
  if (s.page == Page::ClaudeConfirm) {
    claude_answer(false);  // emergency cancel and Home answer "deny"
  } else if (s.page == Page::Confirm) {
    reject_confirmation();
  } else if (s.page == Page::Working) {
    new_flow(Page::Slots);
    toast("Stopped waiting. It may still run: see History.");
  }
}

void open() {
  Guard g;
  if (s.page == Page::Confirm && !s.confirm_sent) {  // left with Home while a confirmation was open
    reject_confirmation();
  }
  wipe(s.act.nonce, sizeof(s.act.nonce));
  wipe(s.job.nonce, sizeof(s.job.nonce));
  // A Claude Code request left open is not answered here: it simply expires on the PC. Nothing of it is kept.
  claude_reset();
  new_flow(Page::Profiles);  // also drops the answer of the cancel above
  start_list_fetch(0);
  claude_start_list();
}

void tile_status(char* out, size_t n) {
  const bool ready = backend_ready();
  Guard g;
  if (ready && !s.have_profiles && s.list != Load::Loading) {
    const uint32_t now = millis();
    // Fills the tile once. After a failure the next look at Home retries, but not before 30 s have passed.
    if (s.list == Load::Idle || now - s.list_try_ms >= kBackgroundRetryMs) start_list_fetch(0);
  }
  if (!ready || (s.list == Load::Failed && s.list_offline)) {
    copy_text(out, n, "offline");
  } else if (s.have_profiles) {
    dk::format_tile_count(s.profiles.count, s.profiles.count >= dk::kMaxProfiles && s.profiles.next_after[0] != '\0', out, n);
  } else {
    copy_text(out, n, s.list == Load::Failed ? "error" : "checking...");
  }
}

}  // namespace

const Ops kDeckOps = {"Control Deck", available, open, view, select, back, key, tick, cancel, call, tile_status};

}  // namespace oyster::modules
