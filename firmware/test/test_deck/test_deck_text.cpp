// Request building and the text shown on the 240x135 screen.
#include <unity.h>

#include <cstring>
#include <set>
#include <string>

#include "oyster_core/deck_json.h"
#include "test_common.h"

using namespace oyster::deck;

namespace {

Job make_job(JobState state, const char* reason = "") {
  Job j;
  std::memset(&j, 0, sizeof(j));
  std::strcpy(j.id, "dj-0a1b2c3d4e5f6071");
  std::strcpy(j.profile_id, "desk");
  j.slot = 8;
  j.state = state;
  j.requires_confirmation = true;
  j.outcome_known = state != JobState::UnknownOutcome;
  std::strcpy(j.executor, "windows");
  std::snprintf(j.reason, sizeof(j.reason), "%s", reason);
  j.step_count = 1;
  std::strcpy(j.steps[0].type, "lock_workstation");
  std::strcpy(j.steps[0].host, "this-pc");
  j.steps[0].outcome = StepOutcome::Pending;
  return j;
}

bool contains(const char* hay, const char* needle) { return std::strstr(hay, needle) != nullptr; }

ApiError no_error() {
  ApiError e;
  std::memset(&e, 0, sizeof(e));
  return e;
}

}  // namespace

static void test_paths_are_built_from_validated_ids_only() {
  char p[160];
  TEST_ASSERT_TRUE(path_profiles(p, sizeof(p), nullptr));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/profiles?limit=5", p);
  TEST_ASSERT_TRUE(path_profiles(p, sizeof(p), ""));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/profiles?limit=5", p);
  TEST_ASSERT_TRUE(path_profiles(p, sizeof(p), "dev-stack"));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/profiles?limit=5&after=dev-stack", p);
  TEST_ASSERT_FALSE(path_profiles(p, sizeof(p), "a&limit=50"));
  TEST_ASSERT_FALSE(path_profiles(p, sizeof(p), "a/b"));
  TEST_ASSERT_FALSE(path_profiles(p, sizeof(p), std::string(41, 'a').c_str()));

  TEST_ASSERT_TRUE(path_run(p, sizeof(p), "desk", 8));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/profiles/desk/slots/8/run", p);
  TEST_ASSERT_FALSE(path_run(p, sizeof(p), "desk", 0));
  TEST_ASSERT_FALSE(path_run(p, sizeof(p), "desk", 10));
  TEST_ASSERT_FALSE(path_run(p, sizeof(p), "../x", 1));
  TEST_ASSERT_FALSE(path_run(p, sizeof(p), "", 1));
  TEST_ASSERT_FALSE(path_run(p, sizeof(p), nullptr, 1));

  TEST_ASSERT_TRUE(path_job(p, sizeof(p), "dj-0a1b"));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/jobs/dj-0a1b", p);
  TEST_ASSERT_TRUE(path_confirm(p, sizeof(p), "dj-0a1b"));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/jobs/dj-0a1b/confirm", p);
  TEST_ASSERT_TRUE(path_cancel(p, sizeof(p), "dj-0a1b"));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/jobs/dj-0a1b/cancel", p);
  TEST_ASSERT_FALSE(path_job(p, sizeof(p), "dj/../../admin"));
  TEST_ASSERT_FALSE(path_job(p, sizeof(p), "dj..1"));
  TEST_ASSERT_FALSE(path_cancel(p, sizeof(p), "a b"));
  TEST_ASSERT_FALSE(path_confirm(p, sizeof(p), "x?y=1"));
  TEST_ASSERT_FALSE(path_job(p, 8, "dj-0a1b2c3d"));  // would not fit: refused, not cut

  TEST_ASSERT_TRUE(path_history(p, sizeof(p), 10));
  TEST_ASSERT_EQUAL_STRING("/v1/deck/history?limit=10", p);
  TEST_ASSERT_FALSE(path_history(p, sizeof(p), 0));
  TEST_ASSERT_FALSE(path_history(p, sizeof(p), 51));
}

static void test_confirm_body() {
  char b[200];
  TEST_ASSERT_TRUE(body_confirm(b, sizeof(b), "dj-1", "Zm9vYmFy-_12", true));
  TEST_ASSERT_EQUAL_STRING("{\"job_id\":\"dj-1\",\"nonce\":\"Zm9vYmFy-_12\",\"approved\":true}", b);
  TEST_ASSERT_TRUE(body_confirm(b, sizeof(b), "dj-1", "Zm9vYmFy-_12", false));
  TEST_ASSERT_TRUE(contains(b, "\"approved\":false"));
  TEST_ASSERT_FALSE(body_confirm(b, sizeof(b), "dj-1", "bad\"nonce", true));
  TEST_ASSERT_FALSE(body_confirm(b, sizeof(b), "dj 1", "Zm9vYmFy", true));
  TEST_ASSERT_FALSE(body_confirm(b, sizeof(b), "dj-1", "", true));
  TEST_ASSERT_FALSE(body_confirm(b, 20, "dj-1", "Zm9vYmFy", true));  // buffer too small: no partial body
}

static void test_idempotency_keys_are_valid_and_distinct() {
  char a[48];
  char b[48];
  format_idempotency_key(a, sizeof(a), 0x01234567u, 0x89abcdefu, 1000u);
  TEST_ASSERT_EQUAL_STRING("dk-0123456789abcdef000003e8", a);
  format_idempotency_key(b, sizeof(b), 0x01234567u, 0x89abcdefu, 1001u);
  TEST_ASSERT_TRUE(std::strcmp(a, b) != 0);
  const size_t n = std::strlen(a);
  TEST_ASSERT_TRUE(n >= 8 && n <= 71);  // backend: 8..80, device buffer: 71
  for (const char* p = a; *p != '\0'; ++p) {
    const char c = *p;
    TEST_ASSERT_TRUE((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || c == '-');
  }
}

static void test_error_texts_for_every_status() {
  char t[160];
  const ApiError none = no_error();
  format_error(-1, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "offline") && contains(t, "Nothing was sent"));
  format_error(-6, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "clock") && contains(t, "Nothing was sent"));
  format_error(-3, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "No usable answer"));
  format_error(401, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "rejected this device"));
  format_error(404, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "Not found"));
  format_error(422, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "422"));
  format_error(503, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "busy"));
  format_error(500, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "HTTP 500"));
  format_error(302, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "Unexpected reply (HTTP 302)"));

  ApiError e = no_error();
  std::strcpy(e.code, "expired");
  format_error(409, e, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "confirmation window closed") && contains(t, "Nothing ran"));
  std::strcpy(e.code, "already_decided");
  format_error(409, e, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "Already decided"));
  std::strcpy(e.code, "not_cancellable");
  format_error(409, e, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "cannot be cancelled"));
  std::strcpy(e.code, "something_new");
  format_error(409, e, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "HTTP 409"));
  std::strcpy(e.code, "rate_limited");
  std::strcpy(e.detail, "too many requests; retry in 12 s");
  format_error(429, e, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "Wait 12 s"));
  format_error(429, none, t, sizeof(t));
  TEST_ASSERT_TRUE(contains(t, "Wait a moment"));
}

static void test_error_text_is_bounded() {
  char t[20];
  ApiError e = no_error();
  format_error(404, e, t, sizeof(t));
  TEST_ASSERT_EQUAL_UINT32(sizeof(t) - 1, std::strlen(t));  // cut, terminated
  format_error(404, e, t, 0);                                // no buffer: no write, no crash
}

static void test_only_ambiguous_transport_errors_keep_the_key() {
  TEST_ASSERT_TRUE(status_may_have_reached_backend(-3));
  TEST_ASSERT_TRUE(status_may_have_reached_backend(-4));
  TEST_ASSERT_TRUE(status_may_have_reached_backend(-5));
  TEST_ASSERT_FALSE(status_may_have_reached_backend(-1));  // offline: nothing was sent
  TEST_ASSERT_FALSE(status_may_have_reached_backend(-6));
  TEST_ASSERT_FALSE(status_may_have_reached_backend(200));
  TEST_ASSERT_FALSE(status_may_have_reached_backend(409));
}

static void test_job_titles_fit_and_alert_on_problems() {
  const JobState all[] = {JobState::Unrecognised, JobState::Queued, JobState::AwaitingConfirmation, JobState::Running,
                          JobState::Succeeded, JobState::Failed, JobState::Cancelled, JobState::Expired,
                          JobState::UnknownOutcome};
  std::set<std::string> seen;
  for (JobState s : all) {
    char t[32];
    const Job j = make_job(s);
    format_job_title(j, t, sizeof(t));
    TEST_ASSERT_TRUE(std::strlen(t) <= 31);
    seen.insert(t);
  }
  TEST_ASSERT_EQUAL_UINT32(9, seen.size());  // every state has its own title
  char t[32];
  format_job_title(make_job(JobState::Failed), t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("!Job failed", t);
  format_job_title(make_job(JobState::UnknownOutcome), t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("!Outcome unknown", t);
  format_job_title(make_job(JobState::Succeeded), t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("Job succeeded", t);
}

static void test_job_body_says_what_happened_and_nothing_more() {
  char b[560];
  Job j = make_job(JobState::Succeeded);
  j.steps[0].outcome = StepOutcome::Succeeded;
  std::strcpy(j.steps[0].summary, "lock request accepted by Windows");
  format_job_body(j, "Lock PC", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Lock PC"));
  TEST_ASSERT_TRUE(contains(b, "1 lock_workstation: succeeded"));
  TEST_ASSERT_TRUE(contains(b, "lock request accepted by Windows"));
  TEST_ASSERT_FALSE(contains(b, "Mock"));

  std::strcpy(j.executor, "mock");
  format_job_body(j, "Lock PC", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Mock executor: nothing was run on the PC."));

  j = make_job(JobState::UnknownOutcome, "timeout");
  j.steps[0].outcome = StepOutcome::Unknown;
  format_job_body(j, "", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "desk/8"));  // no label known: profile and slot
  TEST_ASSERT_TRUE(contains(b, "may have run"));
  TEST_ASSERT_TRUE(contains(b, "never retried"));
  TEST_ASSERT_TRUE(contains(b, "result unknown"));
  TEST_ASSERT_TRUE(contains(b, "the action timed out"));

  j = make_job(JobState::Failed, "needs_admin");
  j.steps[0].outcome = StepOutcome::Failed;
  format_job_body(j, "Restart", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Failed. Nothing was retried."));
  TEST_ASSERT_TRUE(contains(b, "needs administrator rights"));

  j = make_job(JobState::Cancelled, "declined");
  format_job_body(j, "Lock PC", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Rejected on the device. Nothing was run."));
  j = make_job(JobState::Expired, "confirmation_timeout");
  format_job_body(j, "Lock PC", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Nothing was run"));
  TEST_ASSERT_TRUE(contains(b, "confirmation window closed"));
  j = make_job(JobState::Running);
  format_job_body(j, "Lock PC", b, sizeof(b));
  TEST_ASSERT_TRUE(contains(b, "Running on the PC"));
}

static void test_job_body_never_overflows_its_buffer() {
  Job j = make_job(JobState::Failed, "executor_error");
  j.step_count = kMaxSteps;
  for (int i = 0; i < kMaxSteps; ++i) {
    std::strcpy(j.steps[i].type, "service_restart");
    std::memset(j.steps[i].summary, 's', kSummaryCap - 1);
    j.steps[i].summary[kSummaryCap - 1] = '\0';
    j.steps[i].outcome = StepOutcome::Failed;
  }
  std::strcpy(j.executor, "mock");
  char b[560];
  std::memset(b, 'X', sizeof(b));
  format_job_body(j, "A label of 24 chars xxx", b, sizeof(b));
  TEST_ASSERT_TRUE(std::strlen(b) < sizeof(b));
  char small[40];
  format_job_body(j, "A label of 24 chars xxx", small, sizeof(small));
  TEST_ASSERT_EQUAL_UINT32(sizeof(small) - 1, std::strlen(small));
}

static void test_confirm_page_text() {
  char t[40];
  char e[96];
  Job lock = make_job(JobState::AwaitingConfirmation);
  format_confirm_target(lock, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("this PC", t);
  format_confirm_effect(lock, e, sizeof(e));
  TEST_ASSERT_EQUAL_STRING("Locks the Windows session on this PC.", e);

  Job restart = make_job(JobState::AwaitingConfirmation);
  restart.step_count = 2;
  std::strcpy(restart.steps[0].type, "service_status");
  std::strcpy(restart.steps[0].host, "mini-pc");
  std::strcpy(restart.steps[0].service, "nginx");
  std::strcpy(restart.steps[1].type, "service_restart");
  std::strcpy(restart.steps[1].host, "mini-pc");
  std::strcpy(restart.steps[1].service, "nginx");
  format_confirm_target(restart, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("mini-pc", t);
  format_confirm_effect(restart, e, sizeof(e));
  TEST_ASSERT_EQUAL_STRING("Restarts service nginx on mini-pc.", e);  // the read-only step is not what is approved

  Job wake = make_job(JobState::AwaitingConfirmation);
  std::strcpy(wake.steps[0].type, "wake_on_lan");
  std::strcpy(wake.steps[0].host, "nas");
  format_confirm_effect(wake, e, sizeof(e));
  TEST_ASSERT_EQUAL_STRING("Sends a wake-up packet for nas.", e);

  Job other = make_job(JobState::AwaitingConfirmation);
  std::strcpy(other.steps[0].type, "mystery");
  other.steps[0].host[0] = '\0';
  format_confirm_effect(other, e, sizeof(e));
  TEST_ASSERT_EQUAL_STRING("Runs mystery on the host.", e);
  format_confirm_target(other, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("Control Deck", t);
}

static void test_impactful_type_table() {
  TEST_ASSERT_TRUE(is_impactful_type("lock_workstation"));
  TEST_ASSERT_TRUE(is_impactful_type("wake_on_lan"));
  TEST_ASSERT_TRUE(is_impactful_type("service_restart"));
  TEST_ASSERT_FALSE(is_impactful_type("media_key"));
  TEST_ASSERT_FALSE(is_impactful_type("host_health_check"));
  TEST_ASSERT_FALSE(is_impactful_type(""));
  TEST_ASSERT_FALSE(is_impactful_type(nullptr));
}

static void test_rows_fit_the_28_column_list() {
  Profile p;
  std::memset(&p, 0, sizeof(p));
  std::memset(p.name, 'N', kLabelCap - 1);
  p.slot_count = 9;
  char row[64];
  format_profile_row(p, row, sizeof(row));
  TEST_ASSERT_TRUE(std::strlen(row) <= 28);
  TEST_ASSERT_TRUE(contains(row, "(9 slots)"));

  Slot s;
  std::memset(&s, 0, sizeof(s));
  s.number = 8;
  s.impactful = true;
  std::memset(s.label, 'L', kLabelCap - 1);
  format_slot_row(s, row, sizeof(row));
  TEST_ASSERT_TRUE(std::strlen(row) <= 28);
  TEST_ASSERT_EQUAL_CHAR('8', row[0]);
  TEST_ASSERT_EQUAL_CHAR('*', row[1]);
  s.impactful = false;
  format_slot_row(s, row, sizeof(row));
  TEST_ASSERT_EQUAL_CHAR(' ', row[1]);

  HistoryEntry h;
  std::memset(&h, 0, sizeof(h));
  std::memset(h.profile, 'P', kProfileShortCap - 1);
  h.slot = 9;
  h.outcome = HistOutcome::UnknownOutcome;
  format_history_row(h, row, sizeof(row));
  TEST_ASSERT_EQUAL_STRING("PPPPPPPPPPPP/9 UNKNOWN", row);
  TEST_ASSERT_TRUE(std::strlen(row) <= 28);
  h.slot = 0;
  h.outcome = HistOutcome::Denied;
  std::strcpy(h.profile, "desk");
  format_history_row(h, row, sizeof(row));
  TEST_ASSERT_EQUAL_STRING("desk denied", row);
}

static void test_tile_text() {
  char t[16];
  format_tile_count(0, false, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("no profile", t);
  format_tile_count(1, false, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("1 profile", t);
  format_tile_count(2, false, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("2 profiles", t);
  format_tile_count(5, true, t, sizeof(t));
  TEST_ASSERT_EQUAL_STRING("5+ profiles", t);
  TEST_ASSERT_TRUE(std::strlen(t) <= 15);
}

void run_deck_text_tests() {
  RUN_TEST(test_paths_are_built_from_validated_ids_only);
  RUN_TEST(test_confirm_body);
  RUN_TEST(test_idempotency_keys_are_valid_and_distinct);
  RUN_TEST(test_error_texts_for_every_status);
  RUN_TEST(test_error_text_is_bounded);
  RUN_TEST(test_only_ambiguous_transport_errors_keep_the_key);
  RUN_TEST(test_job_titles_fit_and_alert_on_problems);
  RUN_TEST(test_job_body_says_what_happened_and_nothing_more);
  RUN_TEST(test_job_body_never_overflows_its_buffer);
  RUN_TEST(test_confirm_page_text);
  RUN_TEST(test_impactful_type_table);
  RUN_TEST(test_rows_fit_the_28_column_list);
  RUN_TEST(test_tile_text);
}
