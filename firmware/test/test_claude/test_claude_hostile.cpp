// Hostile input for the Claude Code parsers: truncation at every length, byte mutations, deep nesting,
// oversized values, NUL bytes. The parsers must never crash, never write past a struct, and leave a zeroed
// struct when they say no.
#include <unity.h>

#include <cstdint>
#include <cstring>
#include <string>

#include "oyster_core/claude_json.h"
#include "test_common.h"

using namespace oyster::claude;

namespace {

std::string full_reply() {
  std::string s = "{\"approvals\":[";
  for (int i = 0; i < 5; ++i) {
    if (i) s += ",";
    s += "{\"approval_id\":\"appr-0a1b2c3d4e5f607" + std::to_string(i) + "\",\"tool\":\"Bash\",\"summary\":\"npm test && echo " +
         std::to_string(i) + " ...[212]\",\"age_s\":12,\"expires_in_s\":78,\"nonce\":\"Xk3pQ9rT2mVb7LwZcD4aY" +
         std::to_string(i) + "\"}";
  }
  return s + "]}";
}

const char kStatus[] =
    "{\"configured\":true,\"updated_s_ago\":7,\"model\":\"Opus\",\"cost_usd\":0.0123,\"context_pct\":8.0,"
    "\"five_hour_pct\":12.5,\"seven_day_pct\":null}";
const char kDecide[] = "{\"approval_id\":\"appr-0a1b2c3d4e5f6071\",\"state\":\"allowed\",\"decision\":\"allow\"}";

uint32_t g_seed = 4242;
uint32_t next_random() {
  g_seed = g_seed * 1664525u + 1013904223u;
  return g_seed >> 8;
}

template <typename T>
bool all_zero(const T& v) {
  const unsigned char* p = reinterpret_cast<const unsigned char*>(&v);
  for (size_t i = 0; i < sizeof(T); ++i) {
    if (p[i] != 0) return false;
  }
  return true;
}

// Invariants of a successful parse: every string terminated inside its buffer, every value in range.
void check_page(const ApprovalPage& p) {
  TEST_ASSERT_TRUE(p.count <= kMaxApprovals);
  for (uint8_t i = 0; i < p.count; ++i) {
    const Approval& a = p.items[i];
    TEST_ASSERT_TRUE(std::memchr(a.id, 0, sizeof(a.id)) != nullptr);
    TEST_ASSERT_TRUE(std::memchr(a.tool, 0, sizeof(a.tool)) != nullptr);
    TEST_ASSERT_TRUE(std::memchr(a.summary, 0, sizeof(a.summary)) != nullptr);
    TEST_ASSERT_TRUE(std::memchr(a.nonce, 0, sizeof(a.nonce)) != nullptr);
    TEST_ASSERT_TRUE(std::strlen(a.id) >= 1 && std::strlen(a.tool) >= 1);
    TEST_ASSERT_TRUE(std::strlen(a.nonce) >= 8);
    TEST_ASSERT_TRUE(a.expires_in_s <= kMaxExpiresS);
    for (const char* c = a.id; *c; ++c) TEST_ASSERT_TRUE((*c >= '0' && *c <= '9') || (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || *c == '-' || *c == '_');
    for (const char* c = a.summary; *c; ++c) TEST_ASSERT_TRUE(*c >= 0x20 && *c <= 0x7E);
  }
}

void check_status(const Status& s) {
  TEST_ASSERT_TRUE(std::memchr(s.model, 0, sizeof(s.model)) != nullptr);
  if (s.has_cost) TEST_ASSERT_TRUE(s.cost_usd >= 0.0f && s.cost_usd <= 1000000.0f);
  if (s.has_context) TEST_ASSERT_TRUE(s.context_pct >= 0.0f && s.context_pct <= 100.0f);
  if (s.has_five_hour) TEST_ASSERT_TRUE(s.five_hour_pct >= 0.0f && s.five_hour_pct <= 100.0f);
  if (s.has_seven_day) TEST_ASSERT_TRUE(s.seven_day_pct >= 0.0f && s.seven_day_pct <= 100.0f);
}

}  // namespace

static void test_truncated_replies_are_rejected_and_leave_nothing_behind() {
  const std::string reply = full_reply();
  TEST_ASSERT_TRUE(reply.size() < 1500);  // the documented worst case is about 1.3 KiB
  for (size_t n = 0; n < reply.size(); ++n) {
    ApprovalPage p;
    TEST_ASSERT_FALSE(parse_approvals(reply.data(), n, p));
    TEST_ASSERT_TRUE(all_zero(p));
  }
  for (size_t n = 0; n < sizeof(kStatus) - 1; ++n) {
    Status s;
    TEST_ASSERT_FALSE(parse_status(kStatus, n, s));
    TEST_ASSERT_TRUE(all_zero(s));
  }
  for (size_t n = 0; n < sizeof(kDecide) - 1; ++n) {
    DecideReply r;
    TEST_ASSERT_FALSE(parse_decide_reply(kDecide, n, r));
    TEST_ASSERT_TRUE(all_zero(r));
  }
}

static void test_the_untouched_replies_parse() {
  const std::string reply = full_reply();
  ApprovalPage p;
  TEST_ASSERT_TRUE(parse_approvals(reply.data(), reply.size(), p));
  TEST_ASSERT_EQUAL_UINT8(5, p.count);
  check_page(p);
  Status s;
  TEST_ASSERT_TRUE(parse_status(kStatus, sizeof(kStatus) - 1, s));
  check_status(s);
  DecideReply r;
  TEST_ASSERT_TRUE(parse_decide_reply(kDecide, sizeof(kDecide) - 1, r));
}

static void test_random_byte_mutations_never_break_the_invariants() {
  const std::string reply = full_reply();
  for (int round = 0; round < 4000; ++round) {
    std::string a = reply;
    std::string s = kStatus;
    std::string d = kDecide;
    const int edits = 1 + static_cast<int>(next_random() % 3);
    for (int e = 0; e < edits; ++e) {
      a[next_random() % a.size()] = static_cast<char>(next_random() % 256);
      s[next_random() % s.size()] = static_cast<char>(next_random() % 256);
      d[next_random() % d.size()] = static_cast<char>(next_random() % 256);
    }
    ApprovalPage p;
    if (parse_approvals(a.data(), a.size(), p)) {
      check_page(p);
    } else {
      TEST_ASSERT_TRUE(all_zero(p));
    }
    Status st;
    if (parse_status(s.data(), s.size(), st)) {
      check_status(st);
    } else {
      TEST_ASSERT_TRUE(all_zero(st));
    }
    DecideReply r;
    if (parse_decide_reply(d.data(), d.size(), r)) {
      TEST_ASSERT_TRUE(r.state == Decided::Allowed || r.state == Decided::Denied);
      TEST_ASSERT_TRUE(std::memchr(r.approval_id, 0, sizeof(r.approval_id)) != nullptr);
    } else {
      TEST_ASSERT_TRUE(all_zero(r));
    }
  }
}

static void test_deep_nesting_is_refused() {
  std::string deep(200, '[');
  deep += std::string(200, ']');
  ApprovalPage p;
  Status s;
  DecideReply r;
  TEST_ASSERT_FALSE(parse_approvals(deep.data(), deep.size(), p));
  TEST_ASSERT_FALSE(parse_status(deep.data(), deep.size(), s));
  TEST_ASSERT_FALSE(parse_decide_reply(deep.data(), deep.size(), r));
  std::string nested = "{\"approvals\":[{\"approval_id\":";
  for (int i = 0; i < 100; ++i) nested += "{\"a\":";
  nested += "1";
  for (int i = 0; i < 100; ++i) nested += "}";
  nested += "}]}";
  TEST_ASSERT_FALSE(parse_approvals(nested.data(), nested.size(), p));
  TEST_ASSERT_TRUE(all_zero(p));
}

static void test_oversized_values_are_cut_or_refused_never_overrun() {
  const std::string big(50000, 'a');
  ApprovalPage p;
  std::string j = "{\"approvals\":[{\"approval_id\":\"" + big + "\",\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}";
  TEST_ASSERT_FALSE(parse_approvals(j.data(), j.size(), p));
  j = "{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"" + big + "\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}";
  TEST_ASSERT_FALSE(parse_approvals(j.data(), j.size(), p));
  j = "{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":\"" + big + "\"}]}";
  TEST_ASSERT_FALSE(parse_approvals(j.data(), j.size(), p));
  j = "{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"summary\":\"" + big + "\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT32(kSummaryCap - 1, std::strlen(p.items[0].summary));
  // a thousand unknown entries are filtered out before they cost anything
  j = "{\"approvals\":[],\"junk\":[";
  for (int i = 0; i < 1000; ++i) j += (i ? ",1" : "1");
  j += "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT8(0, p.count);
}

static void test_nul_bytes_inside_a_reply_do_not_hide_data() {
  std::string withnul = full_reply();
  withnul[withnul.find("Bash") + 2] = '\0';  // a raw NUL inside a tool name
  ApprovalPage p;
  const bool ok = parse_approvals(withnul.data(), withnul.size(), p);
  if (ok) {
    check_page(p);
  } else {
    TEST_ASSERT_TRUE(all_zero(p));
  }
  // a NUL ahead of the body ends the text for the JSON parser: no parse, no data
  const char lead[] = {'\0', '{', '}', '\0'};
  TEST_ASSERT_FALSE(parse_approvals(lead, 3, p));
  Status s;
  TEST_ASSERT_FALSE(parse_status(lead, 3, s));
}

static void test_null_and_empty_inputs() {
  ApprovalPage p;
  Status s;
  DecideReply r;
  TEST_ASSERT_FALSE(parse_approvals(nullptr, 0, p));
  TEST_ASSERT_FALSE(parse_approvals(nullptr, 10, p));
  TEST_ASSERT_FALSE(parse_approvals("", 0, p));
  TEST_ASSERT_FALSE(parse_status(nullptr, 0, s));
  TEST_ASSERT_FALSE(parse_status("", 0, s));
  TEST_ASSERT_FALSE(parse_decide_reply(nullptr, 3, r));
  TEST_ASSERT_FALSE(parse_decide_reply("", 0, r));
  TEST_ASSERT_TRUE(all_zero(p) && all_zero(s) && all_zero(r));
}

static void test_html_error_pages_and_garbage_are_refused() {
  const char* junk[] = {"<html><body>502 Bad Gateway</body></html>", "null", "true", "12", "\"text\"", "{\"error\":\"x\"}",
                        "{\"approvals\"", "{\"approvals\":[", "{\"approvals\":[{", "\xff\xfe\x00\x01"};
  for (const char* j : junk) {
    ApprovalPage p;
    Status s;
    DecideReply r;
    const size_t n = std::strlen(j);
    TEST_ASSERT_FALSE_MESSAGE(parse_approvals(j, n, p), j);
    TEST_ASSERT_FALSE_MESSAGE(parse_status(j, n, s), j);
    TEST_ASSERT_FALSE_MESSAGE(parse_decide_reply(j, n, r), j);
  }
}

void run_claude_hostile_tests() {
  RUN_TEST(test_truncated_replies_are_rejected_and_leave_nothing_behind);
  RUN_TEST(test_the_untouched_replies_parse);
  RUN_TEST(test_random_byte_mutations_never_break_the_invariants);
  RUN_TEST(test_deep_nesting_is_refused);
  RUN_TEST(test_oversized_values_are_cut_or_refused_never_overrun);
  RUN_TEST(test_nul_bytes_inside_a_reply_do_not_hide_data);
  RUN_TEST(test_null_and_empty_inputs);
  RUN_TEST(test_html_error_pages_and_garbage_are_refused);
}
