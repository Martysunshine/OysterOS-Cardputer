// Parsers for the backend's Control Deck replies: profiles, jobs, history, error bodies.
#include <unity.h>

#include <cstring>
#include <string>

#include "oyster_core/deck_json.h"
#include "test_common.h"

using namespace oyster::deck;

namespace {

const char kProfiles[] = R"JSON({"profiles":[
 {"profile_id":"desk","name":"Desk","slots":[
   {"slot":8,"label":"Lock PC","impactful":true},
   {"slot":2,"label":"Play/Pause","impactful":false},
   {"slot":1,"label":"PC health","impactful":false}]},
 {"profile_id":"dev-stack","name":"Dev stack","slots":[{"slot":1,"label":"Ping","impactful":false}]}],
 "next_after":"dev-stack"})JSON";

const char kAwaiting[] = R"JSON({"job_id":"dj-0a1b2c3d4e5f6071","profile_id":"desk","slot":8,
 "state":"awaiting_confirmation","requires_confirmation":true,"executor":"windows","created_at":1800000000,
 "confirm_deadline":1800000120,"nonce":"Zm9vYmFyYmF6cXV4MTIz","steps":[
 {"index":0,"type":"lock_workstation","host_id":"this-pc","outcome":"pending"}],"duplicate":false})JSON";

const char kRestart[] = R"JSON({"job_id":"dj-1","profile_id":"dev","slot":5,"state":"succeeded",
 "requires_confirmation":true,"executor":"mock","created_at":1800000000,"finished_at":1800000009,"steps":[
 {"index":0,"type":"service_status","host_id":"h1","service":"nginx","outcome":"succeeded","code":"ok","summary":"nginx: running"},
 {"index":1,"type":"service_restart","host_id":"h1","service":"nginx","outcome":"succeeded","code":"ok",
  "summary":"mock executor: nothing was executed on any host"}]})JSON";

const char kUnknown[] = R"JSON({"job_id":"dj-2","profile_id":"desk","slot":8,"state":"unknown_outcome",
 "requires_confirmation":true,"executor":"windows","created_at":1,"reason":"timeout","outcome_known":false,
 "note":"The action may have run.","steps":[{"index":0,"type":"lock_workstation","host_id":"this-pc","outcome":"unknown","code":"timeout"}]})JSON";

const char kHistory[] = R"JSON({"entries":[
 {"seq":9,"ts":5,"event":"job.succeeded","outcome":"succeeded","profile_id":"desk","slot":2,"actions":["media_key"],"job_id":"dj-9"},
 {"seq":8,"ts":5,"event":"job.running","outcome":"pending","profile_id":"desk","slot":2,"job_id":"dj-9"},
 {"seq":7,"ts":5,"event":"job.queued","outcome":"pending","profile_id":"desk","slot":2,"job_id":"dj-9"},
 {"seq":6,"ts":4,"event":"request.denied","outcome":"denied","reason":"empty_slot","profile_id":"desk","slot":9},
 {"seq":5,"ts":3,"event":"job.unknown_outcome","outcome":"unknown_outcome","profile_id":"a-very-long-profile-id","slot":8,"job_id":"dj-5"},
 {"seq":4,"ts":2,"event":"job.cancelled","outcome":"cancelled","profile_id":"desk","slot":8,"job_id":"dj-4"},
 {"seq":3,"ts":1,"event":"job.failed","outcome":"failed","profile_id":"desk","slot":1,"job_id":"dj-3"}],"next_before":3})JSON";

}  // namespace

static void test_profiles_parse_sorted_with_cursor() {
  ProfilePage page;
  TEST_ASSERT_TRUE(parse_profiles(kProfiles, std::strlen(kProfiles), page));
  TEST_ASSERT_EQUAL_UINT8(2, page.count);
  TEST_ASSERT_EQUAL_STRING("desk", page.profiles[0].id);
  TEST_ASSERT_EQUAL_STRING("Desk", page.profiles[0].name);
  TEST_ASSERT_EQUAL_UINT8(3, page.profiles[0].slot_count);
  TEST_ASSERT_EQUAL_UINT8(1, page.profiles[0].slots[0].number);  // ascending, whatever the wire order
  TEST_ASSERT_EQUAL_UINT8(2, page.profiles[0].slots[1].number);
  TEST_ASSERT_EQUAL_UINT8(8, page.profiles[0].slots[2].number);
  TEST_ASSERT_TRUE(page.profiles[0].slots[2].impactful);
  TEST_ASSERT_FALSE(page.profiles[0].slots[0].impactful);
  TEST_ASSERT_EQUAL_STRING("Lock PC", page.profiles[0].slots[2].label);
  TEST_ASSERT_EQUAL_STRING("dev-stack", page.next_after);
  const Slot* s = find_slot(page.profiles[0], 2);
  TEST_ASSERT_NOT_NULL(s);
  TEST_ASSERT_EQUAL_STRING("Play/Pause", s->label);
  TEST_ASSERT_NULL(find_slot(page.profiles[0], 9));
}

static void test_profiles_last_page_and_empty_list() {
  const char last[] = R"({"profiles":[{"profile_id":"p","name":"P","slots":[]}],"next_after":null})";
  ProfilePage page;
  TEST_ASSERT_TRUE(parse_profiles(last, std::strlen(last), page));
  TEST_ASSERT_EQUAL_UINT8(1, page.count);
  TEST_ASSERT_EQUAL_UINT8(0, page.profiles[0].slot_count);
  TEST_ASSERT_EQUAL_STRING("", page.next_after);
  const char none[] = R"({"profiles":[],"next_after":null})";
  TEST_ASSERT_TRUE(parse_profiles(none, std::strlen(none), page));
  TEST_ASSERT_EQUAL_UINT8(0, page.count);
}

static void expect_profiles_rejected(const std::string& json) {
  ProfilePage page;
  page.count = 3;  // must be cleared
  TEST_ASSERT_FALSE(parse_profiles(json.c_str(), json.size(), page));
  TEST_ASSERT_EQUAL_UINT8(0, page.count);
  TEST_ASSERT_EQUAL_STRING("", page.profiles[0].id);
  TEST_ASSERT_EQUAL_STRING("", page.next_after);
}

static std::string one_profile(const std::string& id, const std::string& name, const std::string& slots) {
  return "{\"profiles\":[{\"profile_id\":\"" + id + "\",\"name\":\"" + name + "\",\"slots\":" + slots + "}]}";
}

static void test_profiles_reject_malformed_input() {
  const std::string ok_slots = R"([{"slot":1,"label":"A","impactful":false}])";
  const std::string max_id(40, 'a');
  ProfilePage page;
  std::string good = one_profile(max_id, std::string(24, 'n'), ok_slots);
  TEST_ASSERT_TRUE(parse_profiles(good.c_str(), good.size(), page));  // exactly at the limits
  expect_profiles_rejected(one_profile(std::string(41, 'a'), "N", ok_slots));        // id too long
  expect_profiles_rejected(one_profile("a/b", "N", ok_slots));                       // '/' in an id
  expect_profiles_rejected(one_profile("a b", "N", ok_slots));
  expect_profiles_rejected(one_profile("a?x=1", "N", ok_slots));
  expect_profiles_rejected(one_profile("..", "N", ok_slots));
  expect_profiles_rejected(one_profile("", "N", ok_slots));
  expect_profiles_rejected(one_profile("p", std::string(25, 'n'), ok_slots));        // name too long
  expect_profiles_rejected(one_profile("p", "", ok_slots));
  expect_profiles_rejected(one_profile("p", "Caf\\u00e9", ok_slots));                // not ASCII
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":0,"label":"A","impactful":false}])"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":10,"label":"A","impactful":false}])"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":"1","label":"A","impactful":false}])"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":1,"label":"A","impactful":"no"}])"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":1,"label":"A"}])"));    // impactful is required
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":1,"label":"","impactful":false}])"));
  expect_profiles_rejected(
      one_profile("p", "N", "[{\"slot\":1,\"label\":\"" + std::string(25, 'x') + "\",\"impactful\":false}]"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":1,"label":"tab\there","impactful":false}])"));
  expect_profiles_rejected(one_profile("p", "N", R"([{"slot":2,"label":"A","impactful":false},
                                                     {"slot":2,"label":"B","impactful":false}])"));  // duplicate
  expect_profiles_rejected(R"({"profiles":{"profile_id":"p"}})");
  expect_profiles_rejected(R"({"profiles":"x"})");
  expect_profiles_rejected(R"({"nothing":true})");
  expect_profiles_rejected(R"([])");
  expect_profiles_rejected(R"("text")");
  expect_profiles_rejected("");
  expect_profiles_rejected("not json at all");
  expect_profiles_rejected(R"({"profiles":[],"next_after":5})");
  expect_profiles_rejected(R"({"profiles":[],"next_after":"a/b"})");
}

static void test_profiles_reject_too_many_items() {
  std::string six = "{\"profiles\":[";
  for (int i = 0; i < 6; ++i) {
    if (i > 0) six += ",";
    six += "{\"profile_id\":\"p" + std::to_string(i) + "\",\"name\":\"N\",\"slots\":[]}";
  }
  six += "]}";
  expect_profiles_rejected(six);  // the page size is 5: a longer list is not a valid page
  std::string slots = "[";
  for (int i = 1; i <= 10; ++i) {
    if (i > 1) slots += ",";
    slots += "{\"slot\":" + std::to_string(i % 9 + 1) + ",\"label\":\"x\",\"impactful\":false}";
  }
  slots += "]";
  expect_profiles_rejected(one_profile("p", "N", slots));
}

static void test_job_awaiting_confirmation() {
  Job j;
  TEST_ASSERT_TRUE(parse_job(kAwaiting, std::strlen(kAwaiting), j));
  TEST_ASSERT_EQUAL_STRING("dj-0a1b2c3d4e5f6071", j.id);
  TEST_ASSERT_EQUAL_STRING("desk", j.profile_id);
  TEST_ASSERT_EQUAL_UINT8(8, j.slot);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(JobState::AwaitingConfirmation), static_cast<int>(j.state));
  TEST_ASSERT_TRUE(j.requires_confirmation);
  TEST_ASSERT_TRUE(j.outcome_known);
  TEST_ASSERT_EQUAL_STRING("windows", j.executor);
  TEST_ASSERT_EQUAL_STRING("Zm9vYmFyYmF6cXV4MTIz", j.nonce);
  TEST_ASSERT_EQUAL_UINT32(120, j.confirm_window_s);
  TEST_ASSERT_EQUAL_UINT8(1, j.step_count);
  TEST_ASSERT_EQUAL_STRING("lock_workstation", j.steps[0].type);
  TEST_ASSERT_EQUAL_STRING("this-pc", j.steps[0].host);
  TEST_ASSERT_EQUAL_STRING("", j.steps[0].service);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(StepOutcome::Pending), static_cast<int>(j.steps[0].outcome));
  TEST_ASSERT_FALSE(job_is_terminal(j.state));
  TEST_ASSERT_FALSE(executor_is_mock(j));
}

static void test_job_finished_with_steps_and_mock_executor() {
  Job j;
  TEST_ASSERT_TRUE(parse_job(kRestart, std::strlen(kRestart), j));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(JobState::Succeeded), static_cast<int>(j.state));
  TEST_ASSERT_TRUE(job_is_terminal(j.state));
  TEST_ASSERT_TRUE(executor_is_mock(j));
  TEST_ASSERT_EQUAL_UINT8(2, j.step_count);
  TEST_ASSERT_EQUAL_STRING("nginx", j.steps[1].service);
  TEST_ASSERT_EQUAL_STRING("nginx: running", j.steps[0].summary);
  TEST_ASSERT_EQUAL_STRING("", j.nonce);  // no nonce outside the confirmation window
  TEST_ASSERT_EQUAL_UINT32(0, j.confirm_window_s);
}

static void test_job_unknown_outcome_is_flagged() {
  Job j;
  TEST_ASSERT_TRUE(parse_job(kUnknown, std::strlen(kUnknown), j));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(JobState::UnknownOutcome), static_cast<int>(j.state));
  TEST_ASSERT_FALSE(j.outcome_known);
  TEST_ASSERT_EQUAL_STRING("timeout", j.reason);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(StepOutcome::Unknown), static_cast<int>(j.steps[0].outcome));
  TEST_ASSERT_TRUE(job_is_terminal(j.state));
}

static void test_job_every_state_name_maps() {
  const char* names[] = {"queued", "awaiting_confirmation", "running", "succeeded", "failed", "cancelled", "expired", "unknown_outcome"};
  const JobState states[] = {JobState::Queued, JobState::AwaitingConfirmation, JobState::Running, JobState::Succeeded,
                             JobState::Failed, JobState::Cancelled, JobState::Expired, JobState::UnknownOutcome};
  for (int i = 0; i < 8; ++i) {
    std::string json = std::string("{\"job_id\":\"j1\",\"profile_id\":\"p\",\"slot\":1,\"requires_confirmation\":false,\"state\":\"") +
                       names[i] + "\",\"steps\":[]}";
    Job j;
    TEST_ASSERT_TRUE(parse_job(json.c_str(), json.size(), j));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(states[i]), static_cast<int>(j.state));
  }
  const char odd[] = R"({"job_id":"j1","profile_id":"p","slot":1,"requires_confirmation":false,"state":"teleporting","steps":[]})";
  Job j;
  TEST_ASSERT_TRUE(parse_job(odd, std::strlen(odd), j));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(JobState::Unrecognised), static_cast<int>(j.state));
  TEST_ASSERT_TRUE(job_is_terminal(j.state));  // an unknown state is never polled forever
}

static std::string job_with(const std::string& extra, const std::string& steps = "[]") {
  return "{\"job_id\":\"j1\",\"profile_id\":\"p\",\"slot\":1,\"requires_confirmation\":false,\"state\":\"running\"," + extra +
         "\"steps\":" + steps + "}";
}

static void expect_job_rejected(const std::string& json) {
  Job j;
  j.slot = 7;
  TEST_ASSERT_FALSE(parse_job(json.c_str(), json.size(), j));
  TEST_ASSERT_EQUAL_UINT8(0, j.slot);
  TEST_ASSERT_EQUAL_STRING("", j.id);
  TEST_ASSERT_EQUAL_STRING("", j.nonce);
}

static void test_job_rejects_malformed_replies() {
  Job j;
  std::string ok = job_with("");
  TEST_ASSERT_TRUE(parse_job(ok.c_str(), ok.size(), j));
  expect_job_rejected(R"({"profile_id":"p","slot":1,"state":"running","requires_confirmation":false,"steps":[]})");  // no id
  expect_job_rejected(R"({"job_id":"a/b","profile_id":"p","slot":1,"state":"running","requires_confirmation":false,"steps":[]})");
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":0,"state":"running","requires_confirmation":false,"steps":[]})");
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":10,"state":"running","requires_confirmation":false,"steps":[]})");
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":1,"state":5,"requires_confirmation":false,"steps":[]})");
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":1,"state":"running","steps":[]})");  // flag missing
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":1,"state":"running","requires_confirmation":"yes","steps":[]})");
  expect_job_rejected(R"({"job_id":"j","profile_id":"p","slot":1,"state":"running","requires_confirmation":false})");  // no steps
  expect_job_rejected(job_with("", R"([{"type":"noop"},{"type":"noop"},{"type":"noop"},{"type":"noop"}])"));  // 4 steps
  expect_job_rejected(job_with("", R"([{"host_id":"h"}])"));                                // step without a type
  expect_job_rejected(job_with("", R"([{"type":"x y"}])"));
  expect_job_rejected(job_with("", R"([{"type":"noop","host_id":"a/b"}])"));
  expect_job_rejected(job_with("", R"([{"type":"noop","service":"a b"}])"));
  expect_job_rejected(job_with("\"nonce\":\"short\","));                                    // < 8 characters
  expect_job_rejected(job_with("\"nonce\":\"" + std::string(129, 'n') + "\","));            // > 128
  expect_job_rejected(job_with("\"nonce\":\"abc def gh\","));
  expect_job_rejected(job_with("\"nonce\":12345678,"));
  expect_job_rejected(R"([1,2,3])");
  expect_job_rejected("");
}

static void test_job_cuts_display_text_but_never_identifiers() {
  std::string json = job_with("\"reason\":\"" + std::string(60, 'r') + "\",",
                              "[{\"type\":\"noop\",\"summary\":\"" + std::string(120, 's') + "\",\"code\":\"ok\"}]");
  Job j;
  TEST_ASSERT_TRUE(parse_job(json.c_str(), json.size(), j));
  TEST_ASSERT_EQUAL_UINT32(kCodeCap - 1, std::strlen(j.reason));
  TEST_ASSERT_EQUAL_UINT32(kSummaryCap - 1, std::strlen(j.steps[0].summary));
  const char ctl[] = R"({"job_id":"j","profile_id":"p","slot":1,"state":"failed","requires_confirmation":false,"steps":[
   {"type":"noop","summary":"a\u0001béc\nd"}]})";
  TEST_ASSERT_TRUE(parse_job(ctl, std::strlen(ctl), j));
  for (const char* p = j.steps[0].summary; *p != '\0'; ++p) TEST_ASSERT_TRUE(*p >= 0x20 && *p <= 0x7E);
  TEST_ASSERT_EQUAL_STRING("a?b??c?d", j.steps[0].summary);
}

static void test_job_allows_service_names_with_dots_and_at() {
  const std::string name = std::string("template") + static_cast<char>(64) + "1.service";  // name, '@', instance, dot
  std::string json = job_with("", "[{\"type\":\"service_restart\",\"host_id\":\"h\",\"service\":\"" + name + "\"}]");
  Job j;
  TEST_ASSERT_TRUE(parse_job(json.c_str(), json.size(), j));
  TEST_ASSERT_EQUAL_STRING(name.c_str(), j.steps[0].service);
}

static void test_history_keeps_finished_entries_only() {
  HistoryPage h;
  TEST_ASSERT_TRUE(parse_history(kHistory, std::strlen(kHistory), h));
  TEST_ASSERT_EQUAL_UINT8(5, h.count);  // the two pending rows of job dj-9 are dropped
  TEST_ASSERT_EQUAL_INT(static_cast<int>(HistOutcome::Succeeded), static_cast<int>(h.entries[0].outcome));
  TEST_ASSERT_EQUAL_STRING("dj-9", h.entries[0].job_id);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(HistOutcome::Denied), static_cast<int>(h.entries[1].outcome));
  TEST_ASSERT_EQUAL_STRING("", h.entries[1].job_id);
  TEST_ASSERT_EQUAL_UINT8(9, h.entries[1].slot);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(HistOutcome::UnknownOutcome), static_cast<int>(h.entries[2].outcome));
  TEST_ASSERT_EQUAL_STRING("a-very-long-prof", h.entries[2].profile);  // cut to 16 for the row
  TEST_ASSERT_EQUAL_INT(static_cast<int>(HistOutcome::Cancelled), static_cast<int>(h.entries[3].outcome));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(HistOutcome::Failed), static_cast<int>(h.entries[4].outcome));
}

static void test_history_is_bounded() {
  std::string json = "{\"entries\":[";
  for (int i = 0; i < 30; ++i) {
    if (i > 0) json += ",";
    json += "{\"outcome\":\"failed\",\"profile_id\":\"p\",\"slot\":1,\"job_id\":\"j" + std::to_string(i) + "\"}";
  }
  json += "]}";
  HistoryPage h;
  TEST_ASSERT_TRUE(parse_history(json.c_str(), json.size(), h));
  TEST_ASSERT_EQUAL_UINT8(kMaxHistory, h.count);
  TEST_ASSERT_EQUAL_STRING("j0", h.entries[0].job_id);  // newest first stays first
  std::string huge = "{\"entries\":[";
  for (int i = 0; i < 51; ++i) huge += (i ? ",{\"outcome\":\"failed\"}" : "{\"outcome\":\"failed\"}");
  huge += "]}";
  TEST_ASSERT_FALSE(parse_history(huge.c_str(), huge.size(), h));
  TEST_ASSERT_EQUAL_UINT8(0, h.count);
}

static void test_history_rejects_malformed_entries() {
  HistoryPage h;
  const char noout[] = R"({"entries":[{"profile_id":"p"}]})";
  TEST_ASSERT_FALSE(parse_history(noout, std::strlen(noout), h));
  const char badid[] = R"({"entries":[{"outcome":"failed","job_id":"a/b"}]})";
  TEST_ASSERT_FALSE(parse_history(badid, std::strlen(badid), h));
  const char badprof[] = R"({"entries":[{"outcome":"failed","profile_id":"a b"}]})";
  TEST_ASSERT_FALSE(parse_history(badprof, std::strlen(badprof), h));
  const char notlist[] = R"({"entries":5})";
  TEST_ASSERT_FALSE(parse_history(notlist, std::strlen(notlist), h));
  const char empty[] = R"({"entries":[],"next_before":null})";
  TEST_ASSERT_TRUE(parse_history(empty, std::strlen(empty), h));
  TEST_ASSERT_EQUAL_UINT8(0, h.count);
}

static void test_api_error_body() {
  ApiError e;
  const char body[] = R"({"error":"rate_limited","detail":"too many requests; retry in 7 s"})";
  parse_api_error(body, std::strlen(body), e);
  TEST_ASSERT_EQUAL_STRING("rate_limited", e.code);
  TEST_ASSERT_EQUAL_STRING("too many requests; retry in 7 s", e.detail);
  parse_api_error("garbage", 7, e);
  TEST_ASSERT_EQUAL_STRING("", e.code);
  TEST_ASSERT_EQUAL_STRING("", e.detail);
  const char odd[] = R"({"error":["x"],"detail":{"a":1}})";
  parse_api_error(odd, std::strlen(odd), e);
  TEST_ASSERT_EQUAL_STRING("", e.code);
}

void run_deck_parse_tests() {
  RUN_TEST(test_profiles_parse_sorted_with_cursor);
  RUN_TEST(test_profiles_last_page_and_empty_list);
  RUN_TEST(test_profiles_reject_malformed_input);
  RUN_TEST(test_profiles_reject_too_many_items);
  RUN_TEST(test_job_awaiting_confirmation);
  RUN_TEST(test_job_finished_with_steps_and_mock_executor);
  RUN_TEST(test_job_unknown_outcome_is_flagged);
  RUN_TEST(test_job_every_state_name_maps);
  RUN_TEST(test_job_rejects_malformed_replies);
  RUN_TEST(test_job_cuts_display_text_but_never_identifiers);
  RUN_TEST(test_job_allows_service_names_with_dots_and_at);
  RUN_TEST(test_history_keeps_finished_entries_only);
  RUN_TEST(test_history_is_bounded);
  RUN_TEST(test_history_rejects_malformed_entries);
  RUN_TEST(test_api_error_body);
}
