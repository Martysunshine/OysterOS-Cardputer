// Drives src/modules/deck.cpp (Control Deck module, including its Claude Code row) through its Ops table
// against a scripted fake backend: pages, keys, requests sent, texts shown. The module is compiled into this
// test, so its static state is reachable here and is cleared between scenarios; Arduino.h and esp_system.h in
// this folder stand in for the device headers. No network, no clock: the test moves g_now_ms by hand.
#include <unity.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "test_common.h"

uint32_t g_now_ms = 100000;
uint32_t g_rand = 7;

#include "../../src/modules/deck.cpp"

namespace oyster::modules {

namespace {

struct TReq {
  uint8_t call;
  std::string arg;
};
struct THttp {
  std::string method, path, body, key;
  bool idempotent;
};
struct TReply {
  int status;
  std::string body;
};

std::vector<TReq> t_queue;
std::vector<std::string> t_toasts;
std::vector<THttp> t_http;
std::function<TReply(const THttp&)> t_backend;
bool t_queue_full = false;

}  // namespace

// Framework services the module calls (in the device build they live in modules.cpp).
void lock() {}
void unlock() {}
bool request(Id, uint8_t call_id, const char* arg) {
  if (t_queue_full) return false;
  t_queue.push_back({call_id, arg != nullptr ? arg : ""});
  return true;
}
void changed() {}
void toast(const char* text) { t_toasts.push_back(text); }
bool backend_ready() { return true; }
int http(const char* method, const char* path, const char* body, size_t body_len, char* resp, size_t cap,
         size_t* resp_len, bool idempotent, const char* key) {
  THttp h{method, path, body != nullptr ? std::string(body, body_len) : "", key != nullptr ? key : "", idempotent};
  t_http.push_back(h);
  const TReply r = t_backend(h);
  if (resp_len != nullptr) *resp_len = 0;
  if (resp != nullptr && cap > 0) resp[0] = '\0';
  if (!r.body.empty()) {
    const size_t n = r.body.size() < cap - 1 ? r.body.size() : cap - 1;
    std::memcpy(resp, r.body.data(), n);
    resp[n] = '\0';
    if (resp_len != nullptr) *resp_len = n;
  }
  return r.status;
}
const char* last_error() { return ""; }

namespace {

// ---- helpers --------------------------------------------------------------------------------------------------

bool t_starts(const std::string& s, const char* p) { return s.rfind(p, 0) == 0; }
bool t_has(const std::string& s, const char* p) { return s.find(p) != std::string::npos; }

void t_pump() {
  while (!t_queue.empty()) {
    const TReq r = t_queue.front();
    t_queue.erase(t_queue.begin());
    kDeckOps.call(r.call, r.arg.c_str());
  }
}

PageView t_show() {
  PageView v;
  kDeckOps.view(v);
  return v;
}

std::string t_row(const PageView& v, uint16_t i) {
  char b[64] = {};
  v.row(i, b, sizeof(b));
  return b;
}

// Clears everything the module keeps between calls, as a fresh boot would.
void t_reset() {
  std::memset(&s, 0, sizeof(s));
  std::memset(&cs, 0, sizeof(cs));
  std::memset(&s_cw, 0, sizeof(s_cw));
  t_queue.clear();
  t_http.clear();
  t_toasts.clear();
  t_queue_full = false;
  t_backend = nullptr;
  g_now_ms = 100000;
}

void t_open_with(const std::function<TReply(const THttp&)>& backend) {
  t_reset();
  t_backend = backend;
  kDeckOps.open();
  t_pump();
}

const char kProfileJson[] =
    R"({"profiles":[{"profile_id":"desk","name":"Desk","slots":[{"slot":1,"label":"PC health","impactful":false},)"
    R"({"slot":2,"label":"Play/Pause","impactful":false},{"slot":8,"label":"Lock PC","impactful":true}]}],"next_after":null})";

std::string t_job(const char* id, int slot, const char* state, const char* nonce, const char* type, const char* outcome,
                  const char* executor = "windows", const char* reason = "", const char* summary = "") {
  std::string s = std::string("{\"job_id\":\"") + id + "\",\"profile_id\":\"desk\",\"slot\":" + std::to_string(slot) +
                  ",\"state\":\"" + state + "\",\"requires_confirmation\":" + (nonce[0] ? "true" : "false") +
                  ",\"executor\":\"" + executor + "\",\"created_at\":1800000000";
  if (nonce[0]) s += std::string(",\"confirm_deadline\":1800000120,\"nonce\":\"") + nonce + "\"";
  if (reason[0]) s += std::string(",\"reason\":\"") + reason + "\"";
  s += std::string(",\"steps\":[{\"index\":0,\"type\":\"") + type + "\",\"host_id\":\"this-pc\",\"outcome\":\"" + outcome + "\"";
  if (summary[0]) s += std::string(",\"summary\":\"") + summary + "\"";
  s += "}]}";
  return s;
}

void t_into_slots() {
  PageView v = t_show();
  TEST_ASSERT_TRUE(v.kind == PageKind::List);
  kDeckOps.select(0);  // the profile "desk"
  v = t_show();
  TEST_ASSERT_EQUAL_STRING("Desk", v.title);
  TEST_ASSERT_EQUAL_UINT16(3, v.count);
}

#include "module_cases_deck.h"
#include "module_cases_claude.h"

}  // namespace

void run_module_cases() {
  RUN_TEST(t_profiles_and_tile);
  RUN_TEST(t_media_slot_runs_without_confirmation);
  RUN_TEST(t_lock_confirm_approve);
  RUN_TEST(t_lock_confirm_reject_and_timeout);
  RUN_TEST(t_lost_answer_keeps_the_key);
  RUN_TEST(t_errors_have_plain_texts);
  RUN_TEST(t_watchdog_and_late_answers);
  RUN_TEST(t_history_and_paging);
  RUN_TEST(t_offline_and_empty_lists);
  RUN_TEST(t_claude_row_and_approvals_page);
  RUN_TEST(t_claude_allow_sends_one_decision);
  RUN_TEST(t_claude_deny_by_back_and_by_cancel);
  RUN_TEST(t_claude_one_press_one_decision);
  RUN_TEST(t_claude_window_closes_on_the_confirm_page);
  RUN_TEST(t_claude_lost_answer_is_never_resent);
  RUN_TEST(t_claude_error_replies_have_plain_texts);
  RUN_TEST(t_claude_refreshes_only_while_its_page_is_open);
  RUN_TEST(t_claude_requests_vanish_when_their_window_closes);
  RUN_TEST(t_claude_selection_restarts_when_the_requests_change);
  RUN_TEST(t_claude_usage_page);
  RUN_TEST(t_claude_without_profiles_and_when_not_available);
  RUN_TEST(t_claude_answer_after_leaving_the_page);
  RUN_TEST(t_claude_watchdog_and_busy_worker);
  RUN_TEST(t_claude_nonces_leave_ram_and_never_reach_a_screen);
}

}  // namespace oyster::modules

void run_claude_module_tests() { oyster::modules::run_module_cases(); }
