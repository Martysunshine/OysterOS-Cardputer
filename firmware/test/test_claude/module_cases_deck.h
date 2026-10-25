// Scenarios of the Control Deck module that existed before the Claude Code row (profiles, slots, confirmation,
// lost answers, errors, watchdog, history, offline). Included by test_claude_module.cpp inside its namespace.
// They must keep passing: the only changes against the original run are the extra "Claude Code" row on the
// first page (one more row after the profiles) and the second request sent when the module opens.
#define CHECK(c) TEST_ASSERT_TRUE_MESSAGE((c), #c)

const char kMedia[] = "media_key";  // the action type of the Play/Pause slot

void t_profiles_and_tile() {
  char tile[16];
  t_reset();
  t_backend = [](const THttp&) { return TReply{200, kProfileJson}; };
  kDeckOps.tile_status(tile, sizeof(tile));  // first look at Home: fetches once in the background
  CHECK(std::string(tile) == "checking...");
  CHECK(t_queue.size() == 1);
  kDeckOps.tile_status(tile, sizeof(tile));
  CHECK(t_queue.size() == 1);  // not again while loading
  t_pump();
  kDeckOps.tile_status(tile, sizeof(tile));
  CHECK(std::string(tile) == "1 profile");
  CHECK(t_http[0].method == "GET" && t_http[0].path == "/v1/deck/profiles?limit=5" && t_http[0].idempotent);

  kDeckOps.open();
  t_pump();
  PageView v = t_show();
  CHECK(v.kind == PageKind::List && v.count == 3);  // Desk, Claude Code, History
  CHECK(t_row(v, 0) == "Desk (3 slots)");
  CHECK(t_row(v, 1) == "Claude Code  error");  // this fake backend answers 200 with the profile list for everything
  CHECK(t_row(v, 2) == "History");
  CHECK(std::string(v.left) == "Ent:Open r:Refresh Del:Back");
  CHECK(!kDeckOps.back());  // leaving the module
  kDeckOps.select(0);
  v = t_show();
  CHECK(v.kind == PageKind::List && t_row(v, 0) == "1  PC health" && t_row(v, 2) == "8* Lock PC");
  CHECK(kDeckOps.back() && t_show().count == 3);
}

void t_media_slot_runs_without_confirmation() {
  t_open_with([](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    if (h.method == "POST" && h.path == "/v1/deck/profiles/desk/slots/2/run")
      return {200, t_job("dj-1", 2, "running", "", kMedia, "pending")};
    if (h.method == "GET" && h.path == "/v1/deck/jobs/dj-1")
      return {200, t_job("dj-1", 2, "succeeded", "", kMedia, "succeeded", "windows", "", "play_pause key sent")};
    return {500, ""};
  });
  t_into_slots();
  CHECK(kDeckOps.key(NavEvent::Char, '2'));
  PageView v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Play/Pause" && v.busy);
  t_pump();
  const THttp run = t_http.back();
  CHECK(run.method == "POST" && run.path == "/v1/deck/profiles/desk/slots/2/run");
  CHECK(!run.idempotent);  // never resent by the client
  CHECK(run.key.size() == 27 && t_starts(run.key, "dk-"));
  v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Running");
  CHECK(t_has(v.body, "Play/Pause") && t_has(v.body, "Running on the PC"));
  CHECK(std::string(v.left) == "Waiting... Del:Back");
  g_now_ms += 1000;
  CHECK(!kDeckOps.tick(g_now_ms));  // poll due at +600 ms: sent
  CHECK(t_queue.size() == 1 && t_queue[0].call == 5);
  t_pump();
  v = t_show();
  CHECK(std::string(v.title) == "Job succeeded");
  CHECK(t_has(v.body, "play_pause key sent") && t_has(v.body, "1 media_key" ": succeeded"));
  CHECK(std::string(v.left) == "Del:Back ;.:Scroll");
  g_now_ms += 5000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue.empty());  // a finished job is not polled again
  CHECK(kDeckOps.back() && std::string(t_show().title) == "Desk");
  // a second press is a new action: new key
  const std::string first_key = run.key;
  kDeckOps.key(NavEvent::Char, '2');
  t_pump();
  CHECK(t_http.back().key != first_key);
}

void t_lock_confirm_approve() {
  t_open_with([](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    if (h.path == "/v1/deck/profiles/desk/slots/8/run")
      return {200, t_job("dj-8", 8, "awaiting_confirmation", "Zm9vYmFyYmF6", "lock_workstation", "pending")};
    if (h.path == "/v1/deck/jobs/dj-8/confirm") return {200, t_job("dj-8", 8, "running", "", "lock_workstation", "pending")};
    if (h.path == "/v1/deck/jobs/dj-8")
      return {200, t_job("dj-8", 8, "succeeded", "", "lock_workstation", "succeeded", "windows", "", "lock request accepted by Windows")};
    return {500, ""};
  });
  t_into_slots();
  kDeckOps.select(2);  // Enter on "8* Lock PC"
  t_pump();
  PageView v = t_show();
  CHECK(v.kind == PageKind::Confirm);
  CHECK(std::string(v.confirm_operation) == "Lock PC");
  CHECK(std::string(v.confirm_target) == "this PC");
  CHECK(std::string(v.confirm_effect) == "Locks the Windows session on this PC.");
  CHECK(std::string(v.left) == "Ent:Approve Del:Reject");
  CHECK(t_has(v.right, "s"));
  const size_t before = t_http.size();
  kDeckOps.select(0);  // approve
  kDeckOps.select(0);  // a second Enter before the answer does nothing
  CHECK(t_queue.size() == 1 && t_queue[0].call == 3);
  v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Approved");
  t_pump();
  CHECK(t_http.size() == before + 1);
  const THttp& c = t_http.back();
  CHECK(c.method == "POST" && c.path == "/v1/deck/jobs/dj-8/confirm" && !c.idempotent);
  CHECK(c.body == "{\"job_id\":\"dj-8\",\"nonce\":\"Zm9vYmFyYmF6\",\"approved\":true}");
  v = t_show();
  CHECK(std::string(v.title) == "Running");
  g_now_ms += 1000;
  kDeckOps.tick(g_now_ms);
  t_pump();
  v = t_show();
  CHECK(std::string(v.title) == "Job succeeded" && t_has(v.body, "lock request accepted by Windows"));
  // the nonce is gone from the module: it appears in no page text
  CHECK(!t_has(v.body, "Zm9v") && !t_has(v.title, "Zm9v"));
}

void t_lock_confirm_reject_and_timeout() {
  auto backend = [](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    if (h.path == "/v1/deck/profiles/desk/slots/8/run")
      return {200, t_job("dj-9", 8, "awaiting_confirmation", "Zm9vYmFyYmF6", "lock_workstation", "pending")};
    if (h.path == "/v1/deck/jobs/dj-9/cancel")
      return {200, t_job("dj-9", 8, "cancelled", "", "lock_workstation", "pending", "windows", "cancelled_by_device")};
    return {500, ""};
  };
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '8');
  t_pump();
  CHECK(t_show().kind == PageKind::Confirm);
  CHECK(kDeckOps.back());  // Back rejects
  t_pump();
  CHECK(t_http.back().path == "/v1/deck/jobs/dj-9/cancel");
  for (const THttp& h : t_http) CHECK(h.path.find("/confirm") == std::string::npos);  // never approved
  PageView v = t_show();
  CHECK(std::string(v.title) == "Job cancelled" && t_has(v.body, "Rejected on the device. Nothing was run."));

  // the window runs out: the device rejects by itself
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '8');
  t_pump();
  CHECK(t_show().kind == PageKind::Confirm);
  g_now_ms += 100000;
  kDeckOps.tick(g_now_ms);
  CHECK(t_queue.empty());  // still inside the window: nothing sent
  g_now_ms += 20000;
  CHECK(kDeckOps.tick(g_now_ms));
  CHECK(t_queue.size() == 1 && t_queue[0].call == 4);
  t_pump();
  for (const THttp& h : t_http) CHECK(h.path.find("/confirm") == std::string::npos);
  CHECK(std::string(t_show().title) == "Job cancelled");

  // emergency cancel on the confirmation
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '8');
  t_pump();
  kDeckOps.cancel();
  t_pump();
  CHECK(t_http.back().path == "/v1/deck/jobs/dj-9/cancel");

  // leaving with Home and coming back cancels the leftover
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '8');
  t_pump();
  t_http.clear();
  kDeckOps.open();
  t_pump();
  bool cancelled = false;
  bool profiles = false;
  for (const THttp& h : t_http) {
    cancelled |= h.path == "/v1/deck/jobs/dj-9/cancel";
    profiles |= t_starts(h.path, "/v1/deck/profiles?");
  }
  CHECK(cancelled && profiles);
  CHECK(t_show().kind == PageKind::List);
}

void t_lost_answer_keeps_the_key() {
  int run_calls = 0;
  t_open_with([&](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    if (h.path == "/v1/deck/profiles/desk/slots/2/run") {
      ++run_calls;
      if (run_calls == 1) return {-3, ""};  // i/o error after the request was sent
      return {200, t_job("dj-2", 2, "succeeded", "", kMedia, "succeeded")};
    }
    return {500, ""};
  });
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '2');
  t_pump();
  PageView v = t_show();
  CHECK(std::string(v.title) == "!Status unknown" && t_has(v.body, "may or may not have started"));
  CHECK(std::string(v.left) == "!Ent:Check Del:Back");
  const std::string key1 = t_http.back().key;
  kDeckOps.select(0);  // check again
  t_pump();
  CHECK(t_http.back().key == key1);  // the same key: the backend returns the job it has, or creates it once
  v = t_show();
  CHECK(std::string(v.title) == "Job succeeded");
}

void t_errors_have_plain_texts() {
  int mode = 0;
  t_open_with([&](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    if (mode == 1) return {-1, ""};
    if (mode == 2) return {429, "{\"error\":\"rate_limited\",\"detail\":\"too many requests; retry in 9 s\"}"};
    if (mode == 3) return {404, "{\"error\":\"not_found\",\"detail\":\"unknown id\"}"};
    return {200, "<<<garbage>>>"};
  });
  t_into_slots();
  for (int m = 1; m <= 4; ++m) {
    mode = m;
    kDeckOps.key(NavEvent::Char, '1');
    t_pump();
    PageView v = t_show();
    if (m == 1) CHECK(std::string(v.title) == "!Not done" && t_has(v.body, "Nothing was sent"));
    if (m == 2) CHECK(t_has(v.body, "Wait 9 s"));
    if (m == 3) CHECK(t_has(v.body, "Not found"));
    if (m == 4) CHECK(std::string(v.title) == "!Status unknown");  // an unreadable 200 may have started the job
    CHECK(kDeckOps.back());
    CHECK(std::string(t_show().title) == "Desk");
  }
  mode = 1;
  kDeckOps.key(NavEvent::Char, '1');
  t_queue_full = true;
  kDeckOps.back();
  kDeckOps.key(NavEvent::Char, '1');
  CHECK(t_has(t_show().body, "device is busy"));
}

void t_watchdog_and_late_answers() {
  auto backend = [&](const THttp& h) -> TReply {
    if (t_starts(h.path, "/v1/deck/profiles?")) return {200, kProfileJson};
    return {200, t_job("dj-3", 2, "succeeded", "", kMedia, "succeeded")};
  };
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '2');
  CHECK(t_show().busy);
  g_now_ms += 36000;  // the worker never ran the request
  CHECK(kDeckOps.tick(g_now_ms));
  PageView v = t_show();
  CHECK(std::string(v.title) == "!Status unknown" && !v.busy);
  t_pump();  // the late answer arrives now: dropped
  v = t_show();
  CHECK(std::string(v.title) == "!Status unknown");

  // Back from the waiting page: the late answer must not pull the user to a result page
  t_open_with(backend);
  t_into_slots();
  kDeckOps.key(NavEvent::Char, '2');
  CHECK(kDeckOps.back());
  CHECK(!t_toasts.empty() && t_has(t_toasts.back(), "see History"));
  t_pump();
  CHECK(std::string(t_show().title) == "Desk");
}

void t_history_and_paging() {
  const char page1[] =
      R"({"profiles":[{"profile_id":"a","name":"A","slots":[]},{"profile_id":"b","name":"B","slots":[]}],"next_after":"b"})";
  const char page2[] = R"({"profiles":[{"profile_id":"c","name":"C","slots":[]}],"next_after":null})";
  const char hist[] =
      R"({"entries":[{"seq":3,"event":"job.succeeded","outcome":"succeeded","profile_id":"desk","slot":2,"job_id":"dj-1"},)"
      R"({"seq":2,"event":"job.running","outcome":"pending","profile_id":"desk","slot":2,"job_id":"dj-1"},)"
      R"({"seq":1,"event":"request.denied","outcome":"denied","profile_id":"desk","slot":9}]})";
  t_open_with([&](const THttp& h) -> TReply {
    if (h.path == "/v1/deck/profiles?limit=5") return {200, page1};
    if (h.path == "/v1/deck/profiles?limit=5&after=b") return {200, page2};
    if (h.path == "/v1/deck/history?limit=10") return {200, hist};
    if (h.path == "/v1/deck/jobs/dj-1") return {200, t_job("dj-1", 2, "succeeded", "", kMedia, "succeeded", "mock")};
    return {500, ""};
  });
  PageView v = t_show();
  // A, B, Claude Code, More profiles..., History
  CHECK(v.count == 5 && t_row(v, 0) == "A (0 slots)" && t_row(v, 3) == "More profiles..." && t_row(v, 4) == "History");
  kDeckOps.select(3);
  t_pump();
  v = t_show();
  CHECK(t_http.back().path == "/v1/deck/profiles?limit=5&after=b");
  // C, Claude Code, Previous profiles, History
  CHECK(v.count == 4 && t_row(v, 0) == "C (0 slots)" && t_row(v, 2) == "Previous profiles" && t_row(v, 3) == "History");
  CHECK(std::string(v.right) == "pg 2");
  kDeckOps.select(2);  // back to the first page
  t_pump();
  v = t_show();
  CHECK(t_row(v, 0) == "A (0 slots)");
  kDeckOps.select(4);  // History
  CHECK(t_show().kind == PageKind::List && std::string(t_show().title) == "History");
  t_pump();
  v = t_show();
  CHECK(v.count == 2 && t_row(v, 0) == "desk/2 succeeded" && t_row(v, 1) == "desk/9 denied");
  kDeckOps.select(1);  // the denied entry has no job
  CHECK(!t_toasts.empty() && t_has(t_toasts.back(), "no job"));
  kDeckOps.select(0);
  t_pump();
  v = t_show();
  CHECK(v.kind == PageKind::Text && std::string(v.title) == "Job succeeded");
  CHECK(t_has(v.body, "desk/2") && t_has(v.body, "Mock executor: nothing was run on the PC."));
  CHECK(kDeckOps.back() && std::string(t_show().title) == "History");  // returns to where it came from
  CHECK(kDeckOps.back() && t_show().kind == PageKind::List);
}

void t_offline_and_empty_lists() {
  char tile[16];
  t_open_with([](const THttp&) { return TReply{-1, ""}; });
  PageView v = t_show();
  CHECK(v.count == 0 && std::string(v.empty) == "Backend offline. r:Retry");
  kDeckOps.tile_status(tile, sizeof(tile));
  CHECK(std::string(tile) == "offline");
  CHECK(kDeckOps.key(NavEvent::Char, 'r'));
  CHECK(t_queue.size() == 2);  // the profile list and the Claude Code row are asked again together
  t_backend = [](const THttp& h) {
    if (h.path == "/v1/claude/approvals") return TReply{200, "{\"approvals\":[]}"};
    return TReply{200, "{\"profiles\":[],\"next_after\":null}"};
  };
  t_pump();
  v = t_show();
  CHECK(v.count == 1 && t_row(v, 0) == "Claude Code  none pending");  // no profile: only that row is offered
  CHECK(std::string(v.right) == "no profile");
  kDeckOps.tile_status(tile, sizeof(tile));
  CHECK(std::string(tile) == "no profile");
}
