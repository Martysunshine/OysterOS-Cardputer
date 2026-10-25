// Hostile input: truncation at every length, byte mutations, deep nesting, oversized values, NUL bytes.
// The parsers must never crash, never write past a struct, and leave a zeroed struct when they say no.
#include <unity.h>

#include <cstdint>
#include <cstring>
#include <string>

#include "oyster_core/deck_json.h"
#include "test_common.h"

using namespace oyster::deck;

namespace {

const char kProfilesJson[] =
    R"({"profiles":[{"profile_id":"desk","name":"Desk","slots":[{"slot":1,"label":"PC health","impactful":false},)"
    R"({"slot":8,"label":"Lock PC","impactful":true}]}],"next_after":"zeta"})";

const char kJobJson[] =
    R"({"job_id":"dj-0a1b2c3d4e5f6071","profile_id":"desk","slot":8,"state":"awaiting_confirmation",)"
    R"("requires_confirmation":true,"executor":"windows","created_at":1800000000,"confirm_deadline":1800000120,)"
    R"("nonce":"Zm9vYmFyYmF6cXV4MTIz","steps":[{"index":0,"type":"lock_workstation","host_id":"this-pc",)"
    R"("outcome":"pending","summary":"x"}],"duplicate":false})";

const char kHistoryJson[] =
    R"({"entries":[{"seq":9,"event":"job.succeeded","outcome":"succeeded","profile_id":"desk","slot":2,)"
    R"("job_id":"dj-9"},{"seq":8,"event":"request.denied","outcome":"denied","profile_id":"desk","slot":9}]})";

uint32_t g_seed = 12345;
uint32_t next_random() {
  g_seed = g_seed * 1664525u + 1013904223u;
  return g_seed >> 8;
}

// Invariants of a successful parse: every string terminated inside its buffer, every count in range.
void check_profiles(const ProfilePage& p) {
  TEST_ASSERT_TRUE(p.count <= kMaxProfiles);
  TEST_ASSERT_TRUE(std::memchr(p.next_after, 0, sizeof(p.next_after)) != nullptr);
  for (uint8_t i = 0; i < p.count; ++i) {
    TEST_ASSERT_TRUE(p.profiles[i].slot_count <= kMaxSlots);
    TEST_ASSERT_TRUE(std::memchr(p.profiles[i].id, 0, sizeof(p.profiles[i].id)) != nullptr);
    TEST_ASSERT_TRUE(std::strlen(p.profiles[i].id) >= 1);
    for (uint8_t s = 0; s < p.profiles[i].slot_count; ++s) {
      TEST_ASSERT_TRUE(p.profiles[i].slots[s].number >= 1 && p.profiles[i].slots[s].number <= kMaxSlots);
      TEST_ASSERT_TRUE(std::memchr(p.profiles[i].slots[s].label, 0, kLabelCap) != nullptr);
    }
  }
}

void check_job(const Job& j) {
  TEST_ASSERT_TRUE(j.step_count <= kMaxSteps);
  TEST_ASSERT_TRUE(j.slot >= 1 && j.slot <= kMaxSlots);
  TEST_ASSERT_TRUE(std::strlen(j.id) >= 1 && std::strlen(j.id) < kTokenCap);
  TEST_ASSERT_TRUE(std::strlen(j.nonce) == 0 || std::strlen(j.nonce) >= 8);
  TEST_ASSERT_TRUE(std::strlen(j.nonce) < kNonceCap);
}

void check_history(const HistoryPage& h) { TEST_ASSERT_TRUE(h.count <= kMaxHistory); }

template <typename T>
bool all_zero(const T& v) {
  const unsigned char* p = reinterpret_cast<const unsigned char*>(&v);
  for (size_t i = 0; i < sizeof(T); ++i) {
    if (p[i] != 0) return false;
  }
  return true;
}

}  // namespace

static void test_truncated_replies_are_rejected_and_leave_nothing_behind() {
  for (size_t n = 0; n < sizeof(kProfilesJson) - 1; ++n) {
    ProfilePage p;
    TEST_ASSERT_FALSE(parse_profiles(kProfilesJson, n, p));
    TEST_ASSERT_TRUE(all_zero(p));
  }
  for (size_t n = 0; n < sizeof(kJobJson) - 1; ++n) {
    Job j;
    TEST_ASSERT_FALSE(parse_job(kJobJson, n, j));
    TEST_ASSERT_TRUE(all_zero(j));
  }
  for (size_t n = 0; n < sizeof(kHistoryJson) - 1; ++n) {
    HistoryPage h;
    TEST_ASSERT_FALSE(parse_history(kHistoryJson, n, h));
    TEST_ASSERT_TRUE(all_zero(h));
  }
}

static void test_random_byte_mutations_never_break_the_invariants() {
  for (int round = 0; round < 3000; ++round) {
    std::string pj = kProfilesJson;
    std::string jj = kJobJson;
    std::string hj = kHistoryJson;
    const int edits = 1 + static_cast<int>(next_random() % 3);
    for (int e = 0; e < edits; ++e) {
      pj[next_random() % pj.size()] = static_cast<char>(next_random() % 256);
      jj[next_random() % jj.size()] = static_cast<char>(next_random() % 256);
      hj[next_random() % hj.size()] = static_cast<char>(next_random() % 256);
    }
    ProfilePage p;
    if (parse_profiles(pj.data(), pj.size(), p)) {
      check_profiles(p);
    } else {
      TEST_ASSERT_TRUE(all_zero(p));
    }
    Job j;
    if (parse_job(jj.data(), jj.size(), j)) {
      check_job(j);
    } else {
      TEST_ASSERT_TRUE(all_zero(j));
    }
    HistoryPage h;
    if (parse_history(hj.data(), hj.size(), h)) {
      check_history(h);
    } else {
      TEST_ASSERT_TRUE(all_zero(h));
    }
  }
}

static void test_deep_nesting_is_refused() {
  std::string deep(200, '[');
  deep += std::string(200, ']');
  ProfilePage p;
  Job j;
  HistoryPage h;
  TEST_ASSERT_FALSE(parse_profiles(deep.data(), deep.size(), p));
  TEST_ASSERT_FALSE(parse_job(deep.data(), deep.size(), j));
  TEST_ASSERT_FALSE(parse_history(deep.data(), deep.size(), h));
  std::string nested = "{\"profiles\":";
  for (int i = 0; i < 100; ++i) nested += "{\"a\":";
  nested += "1";
  for (int i = 0; i < 100; ++i) nested += "}";
  nested += "}";
  TEST_ASSERT_FALSE(parse_profiles(nested.data(), nested.size(), p));
}

static void test_oversized_values_are_cut_or_refused_never_overrun() {
  // a megabyte-class string where an identifier is expected is refused; where text is expected it is cut
  std::string big(50000, 'a');
  std::string pj = "{\"profiles\":[{\"profile_id\":\"" + big + "\",\"name\":\"N\",\"slots\":[]}]}";
  ProfilePage p;
  TEST_ASSERT_FALSE(parse_profiles(pj.data(), pj.size(), p));
  std::string jj = "{\"job_id\":\"j\",\"profile_id\":\"p\",\"slot\":1,\"state\":\"failed\",\"requires_confirmation\":false,"
                   "\"reason\":\"" + big + "\",\"executor\":\"" + big + "\",\"steps\":[{\"type\":\"noop\",\"summary\":\"" + big + "\"}]}";
  Job j;
  TEST_ASSERT_TRUE(parse_job(jj.data(), jj.size(), j));
  TEST_ASSERT_EQUAL_UINT32(kCodeCap - 1, std::strlen(j.reason));
  TEST_ASSERT_EQUAL_UINT32(sizeof(j.executor) - 1, std::strlen(j.executor));
  TEST_ASSERT_EQUAL_UINT32(kSummaryCap - 1, std::strlen(j.steps[0].summary));
}

static void test_unknown_and_unrelated_fields_are_ignored() {
  std::string extra = "{\"junk\":[1,2,3,{\"a\":\"b\"}],\"job_id\":\"j1\",\"profile_id\":\"p\",\"slot\":2,\"state\":\"queued\","
                      "\"requires_confirmation\":false,\"future_field\":{\"x\":[true,null]},\"steps\":[{\"type\":\"noop\","
                      "\"unexpected\":123}]}";
  Job j;
  TEST_ASSERT_TRUE(parse_job(extra.data(), extra.size(), j));
  TEST_ASSERT_EQUAL_STRING("j1", j.id);
  TEST_ASSERT_EQUAL_UINT8(1, j.step_count);
}

static void test_nul_bytes_inside_a_reply_do_not_hide_data() {
  std::string withnul(kJobJson);
  withnul[withnul.find("lock_workstation") + 4] = '\0';  // a NUL inside the action type
  Job j;
  const bool ok = parse_job(withnul.data(), withnul.size(), j);
  if (ok) {
    check_job(j);
    TEST_ASSERT_TRUE(std::strlen(j.steps[0].type) < kTypeCap);
  } else {
    TEST_ASSERT_TRUE(all_zero(j));
  }
  // a NUL ahead of the body ends the text for the JSON parser: no parse, no data
  const char lead[] = {'\0', '{', '}', '\0'};
  ProfilePage p;
  TEST_ASSERT_FALSE(parse_profiles(lead, 3, p));
}

static void test_null_and_empty_inputs() {
  ProfilePage p;
  Job j;
  HistoryPage h;
  ApiError e;
  TEST_ASSERT_FALSE(parse_profiles(nullptr, 0, p));
  TEST_ASSERT_FALSE(parse_job(nullptr, 10, j));
  TEST_ASSERT_FALSE(parse_history("", 0, h));
  parse_api_error(nullptr, 0, e);
  TEST_ASSERT_EQUAL_STRING("", e.code);
}

void run_deck_hostile_tests() {
  RUN_TEST(test_truncated_replies_are_rejected_and_leave_nothing_behind);
  RUN_TEST(test_random_byte_mutations_never_break_the_invariants);
  RUN_TEST(test_deep_nesting_is_refused);
  RUN_TEST(test_oversized_values_are_cut_or_refused_never_overrun);
  RUN_TEST(test_unknown_and_unrelated_fields_are_ignored);
  RUN_TEST(test_nul_bytes_inside_a_reply_do_not_hide_data);
  RUN_TEST(test_null_and_empty_inputs);
}
