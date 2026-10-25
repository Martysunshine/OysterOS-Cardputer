// Scenarios of the Claude Code row of the Control Deck module. Included by test_claude_module.cpp inside its
// namespace (CHECK is defined in module_cases_deck.h).

const char kIdA[] = "appr-00000000000000a1";
const char kIdB[] = "appr-00000000000000b2";
const char kNonceA[] = "Nn1AAAAAAAAAAAAAAAAAAA";
const char kNonceB[] = "Nn2BBBBBBBBBBBBBBBBBBB";
const char kStatusFull[] =
    R"({"configured":true,"updated_s_ago":7,"model":"Opus","cost_usd":0.0123,"context_pct":8.0,"five_hour_pct":12.5,"seven_day_pct":3})";
const char kStatusPlain[] =
    R"({"configured":true,"updated_s_ago":7,"model":"Opus","cost_usd":0.0123,"context_pct":8.0,"five_hour_pct":null,"seven_day_pct":null})";

std::string t_appr(const char* id, const char* tool, const char* summary, int exp, const char* nonce) {
  return std::string("{\"approval_id\":\"") + id + "\",\"tool\":\"" + tool + "\",\"summary\":\"" + summary +
         "\",\"age_s\":3,\"expires_in_s\":" + std::to_string(exp) + ",\"nonce\":\"" + nonce + "\"}";
}

std::string t_list(const std::vector<std::string>& items) {
  std::string s = "{\"approvals\":[";
  for (size_t i = 0; i < items.size(); ++i) s += (i ? "," : "") + items[i];
  return s + "]}";
}

std::string t_pad(const std::string& left, const std::string& right) {
  return left + std::string(28 - left.size() - right.size(), ' ') + right;
}

struct Fake {
  std::string profiles = kProfileJson;
  int profiles_status = 200;
  std::string approvals = "{\"approvals\":[]}";
  int approvals_status = 200;
  std::string status = "{\"configured\":false}";
  int status_status = 200;
  int decide_status = 200;
  std::string decide_body;
};
Fake t_fake;

TReply t_serve(const THttp& h) {
  if (t_starts(h.path, "/v1/deck/profiles?")) return {t_fake.profiles_status, t_fake.profiles};
  if (h.path == "/v1/claude/approvals") return {t_fake.approvals_status, t_fake.approvals};
  if (h.path == "/v1/claude/status") return {t_fake.status_status, t_fake.status};
  if (h.method == "POST" && t_starts(h.path, "/v1/claude/approvals/")) {
    if (t_fake.decide_status != 200 || !t_fake.decide_body.empty()) return {t_fake.decide_status, t_fake.decide_body};
    const bool allow = t_has(h.body, "\"decision\":\"allow\"");
    std::string id = h.path.substr(std::strlen("/v1/claude/approvals/"));
    id = id.substr(0, id.find('/'));
    return {200, "{\"approval_id\":\"" + id + "\",\"state\":\"" + (allow ? "allowed" : "denied") + "\",\"decision\":\"" +
                     (allow ? "allow" : "deny") + "\"}"};
  }
  return {500, ""};
}

size_t t_decides() {
  size_t n = 0;
  for (const THttp& h : t_http) n += (h.method == "POST" && t_has(h.path, "/decide")) ? 1 : 0;
  return n;
}

size_t t_claude_gets() {
  size_t n = 0;
  for (const THttp& h : t_http) n += t_starts(h.path, "/v1/claude/") ? 1 : 0;
  return n;
}

bool t_queue_has(uint8_t call) {
  for (const TReq& r : t_queue) {
    if (r.call == call) return true;
  }
  return false;
}

bool t_mem_has(const void* p, size_t n, const char* needle) {
  const size_t m = std::strlen(needle);
  const char* c = static_cast<const char*>(p);
  for (size_t i = 0; i + m <= n; ++i) {
    if (std::memcmp(c + i, needle, m) == 0) return true;
  }
  return false;
}

// Fresh module, the fake backend serving `approvals`; the first page is loaded.
void t_world(const std::string& approvals) {
  t_reset();
  t_fake = Fake{};
  t_fake.approvals = approvals;
  t_backend = t_serve;
  kDeckOps.open();
  t_pump();
}

// Enter on the Claude Code row (row 1: after the profile "desk"), then the first request's confirm page.
void t_to_list() {
  kDeckOps.select(1);
  t_pump();
  CHECK(std::string(t_show().title) == "Claude Code");
}

void t_to_confirm(uint16_t row = 0) {
  t_to_list();
  kDeckOps.select(row);
  CHECK(t_show().kind == PageKind::Confirm);
}

void t_claude_row_and_approvals_page() {
  t_reset();
  t_fake = Fake{};
  t_fake.approvals = t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA), t_appr(kIdB, "Edit", "", 30, kNonceB)});
  t_fake.status = kStatusPlain;
  t_backend = t_serve;
  kDeckOps.open();
  t_pump();
  PageView v = t_show();
  CHECK(v.count == 3);  // Desk, Claude Code, History
  CHECK(t_row(v, 1) == "Claude Code  2 pending");
  CHECK(t_claude_gets() == 1);  // one extra call when the module opens ...
  t_show();
  t_show();
  kDeckOps.tick(g_now_ms);
  CHECK(t_claude_gets() == 1 && t_queue.empty());  // ... not on a redraw, not on a tick of the first page

  kDeckOps.select(1);
  t_pump();
  v = t_show();
  CHECK(v.kind == PageKind::List && std::string(v.title) == "Claude Code" && v.count == 3);
  CHECK(t_row(v, 0) == t_pad("Bash npm test", "78s"));
  CHECK(t_row(v, 1) == t_pad("Edit (no details)", "30s"));
  CHECK(t_row(v, 2) == "Usage Opus $0.0123 ctx 8%");
  CHECK(std::string(v.left) == "Ent:Open s:Usage r:Refresh");
  CHECK(!v.busy);
  g_now_ms += 10000;  // the remaining time follows the clock without asking the backend
  v = t_show();
  CHECK(t_row(v, 0) == t_pad("Bash npm test", "68s"));
  CHECK(t_row(v, 1) == t_pad("Edit (no details)", "20s"));
  CHECK(kDeckOps.back() && t_show().count == 3);  // back to the first page
}

void t_claude_allow_sends_one_decision() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  PageView v = t_show();
  CHECK(std::string(v.confirm_operation) == "Claude Code: Bash");
  CHECK(std::string(v.confirm_target) == "Details shortened");
  CHECK(std::string(v.confirm_effect) == "npm test\nAllow once. Deny what you cannot judge.");
  CHECK(std::string(v.left) == "Ent:Allow once Del:Deny");
  CHECK(std::string(v.right) == "78s");
  const size_t before = t_http.size();
  kDeckOps.select(0);
  v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Sending answer" && v.busy);
  kDeckOps.select(0);  // a second Enter does nothing
  CHECK(t_queue.size() == 1 && t_queue[0].call == 9 && t_queue[0].arg.substr(2) == "a");
  t_pump();
  CHECK(t_http.size() == before + 1 && t_decides() == 1);
  const THttp& d = t_http.back();
  CHECK(d.method == "POST" && d.path == "/v1/claude/approvals/appr-00000000000000a1/decide");
  CHECK(!d.idempotent && d.key.empty());  // never resent by the client
  CHECK(d.body == "{\"decision\":\"allow\",\"nonce\":\"Nn1AAAAAAAAAAAAAAAAAAA\"}");
  v = t_show();
  CHECK(std::string(v.title) == "Allowed" && t_has(v.body, "Claude Code: Bash") && t_has(v.body, "Allowed once."));
  CHECK(!v.busy);
  CHECK(!t_has(v.body, "Nn1AAAA") && !t_has(v.title, "Nn1AAAA"));
  CHECK(!t_mem_has(&cs, sizeof(cs), kNonceA) && !t_mem_has(&s_cw, sizeof(s_cw), kNonceA));  // wiped
  CHECK(cs.list.count == 0);  // the answered request left the list at once
  CHECK(kDeckOps.back());  // to the list, which is read again
  CHECK(t_queue_has(7));
  t_fake.approvals = "{\"approvals\":[]}";
  t_pump();
  v = t_show();
  CHECK(v.count == 2 && t_row(v, 0) == "No requests pending");
}

void t_claude_deny_by_back_and_by_cancel() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  CHECK(kDeckOps.back());  // Back = deny
  CHECK(t_queue.size() == 1 && t_queue[0].call == 9 && t_queue[0].arg.substr(2) == "d");
  t_pump();
  CHECK(t_decides() == 1 && t_http.back().body == "{\"decision\":\"deny\",\"nonce\":\"Nn1AAAAAAAAAAAAAAAAAAA\"}");
  CHECK(std::string(t_show().title) == "Denied" && t_has(t_show().body, "Denied."));

  // emergency cancel (and Home, which calls the same handler) = deny
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  kDeckOps.cancel();
  t_pump();
  CHECK(t_decides() == 1 && t_has(t_http.back().body, "\"decision\":\"deny\""));
  CHECK(std::string(t_show().title) == "Denied");

  // cancel on the list sends nothing
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_list();
  kDeckOps.cancel();
  t_pump();
  CHECK(t_decides() == 0);
}

void t_claude_one_press_one_decision() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  kDeckOps.select(0);  // allow
  kDeckOps.select(0);  // a second Enter, an emergency cancel and a key press add no second answer
  kDeckOps.cancel();
  kDeckOps.key(NavEvent::Char, 'r');
  t_pump();
  CHECK(t_decides() == 1 && t_has(t_http.back().body, "\"decision\":\"allow\""));
  CHECK(std::string(t_show().title) == "Allowed");
}

void t_claude_window_closes_on_the_confirm_page() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  g_now_ms += 76000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_show().kind == PageKind::Confirm && std::string(t_show().right) == "2s");
  g_now_ms += 2001;  // 78.001 s after the list was read
  CHECK(kDeckOps.tick(g_now_ms));
  PageView v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "!Expired" && t_has(v.body, "Answer it at the PC"));
  CHECK(!t_mem_has(&cs, sizeof(cs), kNonceA));  // wiped
  kDeckOps.select(0);  // Enter on the expired page answers nothing
  t_pump();
  CHECK(t_decides() == 0);
  t_fake.approvals = "{\"approvals\":[]}";
  CHECK(kDeckOps.back());
  t_pump();
  CHECK(t_row(t_show(), 0) == "No requests pending");
}

void t_claude_lost_answer_is_never_resent() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_fake.decide_status = -3;  // the connection broke after the request left
  t_to_confirm();
  kDeckOps.select(0);
  t_pump();
  PageView v = t_show();
  CHECK(std::string(v.title) == "!Status unknown" && t_has(v.body, "NOT sent again"));
  CHECK(cs.list.count == 1);  // it may still be pending: the list decides, not a guess
  for (int i = 0; i < 20; ++i) {
    g_now_ms += 3000;
    kDeckOps.tick(g_now_ms);
    kDeckOps.select(0);
    t_pump();
  }
  CHECK(t_decides() == 1);  // nothing came back, nothing was repeated
  CHECK(kDeckOps.back());   // a new answer needs a new deliberate press
  t_pump();
  CHECK(t_decides() == 1);

  // an unreadable 200 is the same: the answer may or may not have been recorded
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_fake.decide_body = "<<<garbage>>>";
  t_to_confirm();
  kDeckOps.select(0);
  t_pump();
  CHECK(std::string(t_show().title) == "!Status unknown");
  CHECK(t_decides() == 1);
}

void t_claude_error_replies_have_plain_texts() {
  struct Case {
    int status;
    const char* body;
    const char* title;
    bool gone;  // the request left the list
    const char* text;
  };
  const Case cases[] = {
      {409, R"({"error":"expired","detail":"the approval window has closed"})", "!Too late", true, "timed out"},
      {409, R"({"error":"already_decided","detail":"x"})", "!Already answered", true, "Already answered"},
      {403, R"({"error":"forbidden","detail":"decision rejected"})", "!Rejected", false, "stale"},
      {401, R"({"error":"unauthorized","detail":"x"})", "!Rejected", false, "Enrol"},
      {404, R"({"error":"not_found","detail":"unknown id"})", "!No longer there", true, "no longer exists"},
      {429, R"({"error":"rate_limited","detail":"too many requests; retry in 9 s"})", "!Rate limit", false, "Wait 9 s"},
      {-1, "", "!Not sent", false, "NOT sent"},
      {-2, "", "!Not sent", false, "radio is busy"},
      {-6, "", "!Not sent", false, "clock is not set"},
      {500, "", "!Not done", false, "HTTP 500"},
      {503, R"({"error":"busy","detail":"x"})", "!Not done", false, "NOT sent again"},
  };
  for (const Case& c : cases) {
    t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
    t_fake.decide_status = c.status;
    t_fake.decide_body = c.body;
    t_to_confirm();
    kDeckOps.select(0);
    t_pump();
    const PageView v = t_show();
    TEST_ASSERT_EQUAL_STRING(c.title, v.title);
    TEST_ASSERT_TRUE_MESSAGE(t_has(v.body, c.text), v.body);
    TEST_ASSERT_TRUE(t_has(v.body, "Claude Code: Bash"));
    TEST_ASSERT_EQUAL_UINT8(c.gone ? 0 : 1, cs.list.count);
    CHECK(t_decides() == 1);
    CHECK(!t_mem_has(&cs, sizeof(cs), kNonceA) || !c.gone);
    CHECK(!t_has(v.body, "Nn1AAAA"));
    CHECK(kDeckOps.back());  // every failure page can be left with Back
    CHECK(std::string(t_show().title) == "Claude Code");
  }
}

void t_claude_refreshes_only_while_its_page_is_open() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 1000, kNonceA)}));
  const size_t base = t_claude_gets();
  g_now_ms += 60000;  // first page, Home, another module: nothing is polled
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue.empty() && t_claude_gets() == base);

  kDeckOps.select(1);
  t_pump();
  const size_t entered = t_claude_gets();
  CHECK(entered == base + 2);  // read again on entry (list + usage)
  g_now_ms += 2900;
  kDeckOps.tick(g_now_ms);
  CHECK(!t_queue_has(7));
  g_now_ms += 100;  // 3 s
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue_has(7));
  kDeckOps.tick(g_now_ms);  // one request at a time: no second one while the first is out
  CHECK(t_queue.size() == 1);
  t_pump();
  CHECK(t_claude_gets() == entered + 1);
  g_now_ms += 15000;  // the usage line has its own, slower cadence
  kDeckOps.tick(g_now_ms);
  t_pump();
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue_has(8));
  t_pump();

  CHECK(kDeckOps.back());  // first page again: the polling stops
  const size_t left = t_claude_gets();
  g_now_ms += 30000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue.empty() && t_claude_gets() == left);
}

void t_claude_requests_vanish_when_their_window_closes() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 5, kNonceA), t_appr(kIdB, "Edit", "a.txt", 30, kNonceB)}));
  t_to_list();
  CHECK(t_show().count == 3);
  g_now_ms += 6000;  // the first window closed; no refresh has happened
  CHECK(kDeckOps.tick(g_now_ms));
  PageView v = t_show();
  CHECK(v.count == 2 && t_has(t_row(v, 0), "Edit a.txt") && t_has(t_row(v, 0), "24s"));
  t_queue.clear();
  g_now_ms += 25000;
  kDeckOps.tick(g_now_ms);
  v = t_show();
  CHECK(v.count == 2 && t_row(v, 0) == "No requests pending");
  // the row on the first page follows
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 5, kNonceA), t_appr(kIdB, "Edit", "a.txt", 30, kNonceB)}));
  g_now_ms += 6000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_row(t_show(), 1) == "Claude Code  1 pending");
}

void t_claude_selection_restarts_when_the_requests_change() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)}));
  t_to_list();
  const uint8_t page = t_show().page_id;
  g_now_ms += 1000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_show().page_id == page);  // time passing does not move the selection
  t_fake.approvals = t_list({t_appr(kIdB, "Write", "b.txt", 900, kNonceB), t_appr(kIdA, "Bash", "npm test", 900, kNonceA)});
  CHECK(kDeckOps.key(NavEvent::Char, 'r'));
  t_pump();
  const uint8_t after = t_show().page_id;
  CHECK(after != page && t_show().count == 3);  // a new request: back to the first row, which is the new one
  CHECK(t_has(t_row(t_show(), 0), "Write b.txt"));
  kDeckOps.key(NavEvent::Char, 'r');
  t_pump();
  CHECK(t_show().page_id == after);  // the same requests again: nothing moves
}

void t_claude_usage_page() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)}));
  CHECK(kDeckOps.key(NavEvent::Char, 's'));  // from the first page
  t_pump();
  PageView v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Claude Code usage");
  CHECK(std::string(v.body) == "No Claude Code status received yet (see docs/CLAUDE_CODE_LINK.md).");
  CHECK(kDeckOps.back() && t_show().kind == PageKind::List);  // back where it came from

  t_fake.status = kStatusFull;
  kDeckOps.select(1);
  t_pump();
  CHECK(kDeckOps.key(NavEvent::Char, 's'));  // from the approvals page
  t_pump();
  v = t_show();
  CHECK(t_has(v.body, "Model: Opus") && t_has(v.body, "Cost (estimate): $0.0123") && t_has(v.body, "Context used: 8%"));
  CHECK(t_has(v.body, "5-hour plan used: 12.5%") && t_has(v.body, "7-day plan used: 3%") && t_has(v.body, "Updated 7 s ago."));
  g_now_ms += 65000;
  CHECK(t_has(t_show().body, "Updated 1 min ago."));  // the age keeps counting between refreshes
  CHECK(std::string(v.left) == "r:Refresh Del:Back");

  t_fake.status = kStatusPlain;  // plan numbers the backend does not send are not shown
  CHECK(kDeckOps.key(NavEvent::Char, 'r'));
  t_pump();
  v = t_show();
  CHECK(!t_has(v.body, "plan") && t_has(v.body, "Context used: 8%"));
  g_now_ms += 10000;  // the page asks again by itself
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue_has(8));
  t_fake.status_status = 404;
  t_pump();
  v = t_show();
  CHECK(t_has(v.body, "Model: Opus") && t_has(v.body, "Refresh failed: Not set up on the backend"));
  CHECK(kDeckOps.back() && t_show().kind == PageKind::List);

  // Enter on the usage row of the approvals page opens it too
  kDeckOps.select(1);
  CHECK(std::string(t_show().title) == "Claude Code usage");

  // a status that never arrived
  t_world("{\"approvals\":[]}");
  t_fake.status_status = -1;
  kDeckOps.key(NavEvent::Char, 's');
  t_pump();
  CHECK(t_has(t_show().body, "Could not read the usage line.") && t_has(t_show().body, "Backend offline"));
}

void t_claude_without_profiles_and_when_not_available() {
  // no profile for this device: the Claude Code row is still there
  t_reset();
  t_fake = Fake{};
  t_fake.profiles = R"({"profiles":[],"next_after":null})";
  t_fake.approvals = t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)});
  t_backend = t_serve;
  kDeckOps.open();
  t_pump();
  PageView v = t_show();
  CHECK(v.count == 1 && t_row(v, 0) == "Claude Code  1 pending" && std::string(v.right) == "no profile");
  kDeckOps.select(0);
  CHECK(std::string(t_show().title) == "Claude Code");

  // a backend without the link (404), a device it does not accept (401), no network
  struct Case {
    int status;
    const char* row;
    const char* note;
  };
  const Case cases[] = {{404, "Claude Code  not set up", "Not set up on the backend"},
                        {401, "Claude Code  not accepted", "Device not accepted"},
                        {403, "Claude Code  not accepted", "Device not accepted"},
                        {-1, "Claude Code  offline", "Backend offline. r:Retry"},
                        {-3, "Claude Code  offline", "Backend offline. r:Retry"},
                        {500, "Claude Code  error", "Backend error. r:Retry"}};
  for (const Case& c : cases) {
    t_reset();
    t_fake = Fake{};
    t_fake.approvals_status = c.status;
    t_backend = t_serve;
    kDeckOps.open();
    t_pump();
    v = t_show();
    TEST_ASSERT_EQUAL_STRING(c.row, t_row(v, 1).c_str());
    kDeckOps.select(1);
    t_pump();
    v = t_show();
    TEST_ASSERT_EQUAL_STRING(c.note, t_row(v, 0).c_str());
    TEST_ASSERT_EQUAL_UINT16(2, v.count);  // the note and the usage line
    kDeckOps.select(0);                    // the note is not an action
    CHECK(std::string(t_show().title) == "Claude Code");
  }
  // unreadable reply: an error, not "offline"
  t_reset();
  t_fake = Fake{};
  t_fake.approvals = "<<<garbage>>>";
  t_backend = t_serve;
  kDeckOps.open();
  t_pump();
  CHECK(t_row(t_show(), 1) == "Claude Code  error");
}

void t_claude_answer_after_leaving_the_page() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 78, kNonceA)}));
  t_to_confirm();
  kDeckOps.select(0);
  CHECK(kDeckOps.back());  // left while the answer is on its way
  CHECK(std::string(t_show().title) == "Claude Code" && !t_toasts.empty() && t_has(t_toasts.back(), "may still arrive"));
  t_pump();  // the reply comes in: the page does not jump, the owner is told how it ended
  CHECK(std::string(t_show().title) == "Claude Code");
  CHECK(!t_toasts.empty() && t_toasts.back() == "Claude Code: Allowed");
  CHECK(t_decides() == 1);
}

void t_claude_watchdog_and_busy_worker() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)}));
  t_to_confirm();
  kDeckOps.select(0);  // the worker never gets to it
  g_now_ms += 36000;
  CHECK(kDeckOps.tick(g_now_ms));
  PageView v = t_show();
  CHECK(std::string(v.title) == "!Status unknown" && t_has(v.body, "NOT sent again") && !v.busy);
  t_pump();  // the late reply is dropped: the page does not change
  CHECK(std::string(t_show().title) == "!Status unknown");
  CHECK(!t_toasts.empty() && t_toasts.back() == "Claude Code: Allowed");

  // worker queue full: the answer is refused at once, plainly, and the nonce is gone
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)}));
  t_to_confirm();
  t_queue_full = true;
  kDeckOps.select(0);
  v = t_show();
  CHECK(std::string(v.title) == "!Not sent" && t_has(v.body, "NOT sent"));
  CHECK(!t_mem_has(&cs.cur, sizeof(cs.cur), kNonceA) && !t_mem_has(cs.act_nonce, sizeof(cs.act_nonce), kNonceA));
  t_queue_full = false;
  CHECK(t_decides() == 0);

  // a refresh in flight is not "working"; an answer in flight is
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA)}));
  t_to_list();
  g_now_ms += 3000;
  kDeckOps.tick(g_now_ms);
  CHECK(!t_queue.empty() && !t_show().busy);
  t_pump();
  kDeckOps.select(0);
  kDeckOps.select(0);
  CHECK(t_show().busy);
  t_pump();
}

void t_claude_nonces_leave_ram_and_never_reach_a_screen() {
  t_world(t_list({t_appr(kIdA, "Bash", "npm test", 900, kNonceA), t_appr(kIdB, "Edit", "b.txt", 900, kNonceB)}));
  std::string seen;  // every text the module drew
  auto look = [&](const PageView& v) {
    seen += std::string(v.title) + "|" + v.body + "|" + v.left + "|" + v.right + "|" + v.confirm_operation + "|" +
            v.confirm_target + "|" + v.confirm_effect + "|";
    for (uint16_t i = 0; v.kind == PageKind::List && i < v.count && v.row != nullptr; ++i) seen += t_row(v, i) + "|";
  };
  look(t_show());
  kDeckOps.select(1);
  t_pump();
  look(t_show());
  kDeckOps.select(0);
  look(t_show());
  CHECK(t_mem_has(&cs, sizeof(cs), kNonceA));  // it is held while the request is open ...
  kDeckOps.select(0);
  look(t_show());
  t_pump();
  look(t_show());
  CHECK(!t_mem_has(&cs, sizeof(cs), kNonceA));  // ... and gone once it was used
  CHECK(t_mem_has(&cs, sizeof(cs), kNonceB));   // the other request is still pending
  CHECK(!t_has(seen, "Nn1") && !t_has(seen, "Nn2"));
  kDeckOps.back();
  kDeckOps.back();
  kDeckOps.open();  // entering the module again keeps nothing of the last visit
  CHECK(!t_mem_has(&cs, sizeof(cs), kNonceB) && !t_mem_has(&s_cw, sizeof(s_cw), kNonceB));
  t_queue.clear();
  CHECK(sizeof(cs) <= 2048);  // the state this row adds
  char sizes[96];
  std::snprintf(sizes, sizeof(sizes), "Claude Code row RAM: state %u bytes, worker scratch %u bytes",
                static_cast<unsigned>(sizeof(cs)), static_cast<unsigned>(sizeof(s_cw)));
  TEST_MESSAGE(sizes);
}
