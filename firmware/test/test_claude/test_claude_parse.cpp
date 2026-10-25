// Parsers and request builders of the Claude Code link (oyster_core/claude_json.h).
#include <unity.h>

#include <cstring>
#include <string>

#include "oyster_core/claude_json.h"
#include "test_common.h"

using namespace oyster::claude;

namespace {

const char kId[] = "appr-0a1b2c3d4e5f6071";
const char kNonce[] = "Xk3pQ9rT2mVb7LwZcD4aYe";

std::string item(const std::string& id = kId, const std::string& tool = "Bash", const std::string& summary = "npm test",
                 const std::string& age = "12", const std::string& exp = "78", const std::string& nonce = kNonce) {
  return "{\"approval_id\":\"" + id + "\",\"tool\":\"" + tool + "\",\"summary\":\"" + summary + "\",\"age_s\":" + age +
         ",\"expires_in_s\":" + exp + ",\"nonce\":\"" + nonce + "\"}";
}

std::string list_of(const std::string& one, int n) {
  std::string s = "{\"approvals\":[";
  for (int i = 0; i < n; ++i) s += (i ? "," : "") + one;
  return s + "]}";
}

template <typename T>
bool all_zero(const T& v) {
  const unsigned char* p = reinterpret_cast<const unsigned char*>(&v);
  for (size_t i = 0; i < sizeof(T); ++i) {
    if (p[i] != 0) return false;
  }
  return true;
}

bool parses(const std::string& json) {
  ApprovalPage p;
  return parse_approvals(json.data(), json.size(), p);
}

// A rejected parse must also leave a zeroed struct behind.
void expect_rejected(const std::string& json) {
  ApprovalPage p;
  TEST_ASSERT_FALSE_MESSAGE(parse_approvals(json.data(), json.size(), p), json.c_str());
  TEST_ASSERT_TRUE(all_zero(p));
}

}  // namespace

static void test_empty_list() {
  ApprovalPage p;
  const std::string j = "{\"approvals\":[]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT8(0, p.count);
}

static void test_one_item_is_read_field_by_field() {
  ApprovalPage p;
  const std::string j = "{\"approvals\":[" + item() + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT8(1, p.count);
  TEST_ASSERT_EQUAL_STRING(kId, p.items[0].id);
  TEST_ASSERT_EQUAL_STRING("Bash", p.items[0].tool);
  TEST_ASSERT_EQUAL_STRING("npm test", p.items[0].summary);
  TEST_ASSERT_EQUAL_STRING(kNonce, p.items[0].nonce);
  TEST_ASSERT_EQUAL_UINT32(12, p.items[0].age_s);
  TEST_ASSERT_EQUAL_UINT32(78, p.items[0].expires_in_s);
}

static void test_five_items_keep_their_order_and_six_are_refused() {
  std::string j = "{\"approvals\":[";
  for (int i = 0; i < 5; ++i) {
    j += (i ? "," : "") + item(std::string("appr-000000000000000") + char('0' + i), "Bash", "cmd " + std::to_string(i));
  }
  j += "]}";
  ApprovalPage p;
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT8(5, p.count);
  for (int i = 0; i < 5; ++i) {
    TEST_ASSERT_EQUAL_STRING(("cmd " + std::to_string(i)).c_str(), p.items[i].summary);
  }
  expect_rejected(list_of(item(), 6));
}

static void test_missing_or_null_summary_is_empty_not_an_error() {
  ApprovalPage p;
  std::string j = "{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Edit\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_STRING("", p.items[0].summary);
  TEST_ASSERT_EQUAL_UINT32(0, p.items[0].age_s);  // age is optional
  j = "{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Edit\",\"summary\":null,\"age_s\":null,\"expires_in_s\":9,"
      "\"nonce\":\"abcdefgh\"}]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_STRING("", p.items[0].summary);
}

static void test_required_fields_are_required() {
  expect_rejected("{\"approvals\":[{\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}");          // id
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}");     // tool
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"nonce\":\"abcdefgh\"}]}");      // expiry
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"expires_in_s\":9}]}");         // nonce
  expect_rejected("{\"approvals\":[{}]}");
  expect_rejected("{}");
  expect_rejected("{\"approvals\":null}");
  expect_rejected("[]");
}

static void test_wrong_types_are_refused() {
  expect_rejected(item() );  // not wrapped: top level is an object without "approvals"
  expect_rejected("{\"approvals\":" + item() + "}");                          // an object, not an array
  expect_rejected("{\"approvals\":\"none\"}");
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "12", "\"78\"") + "]}");   // expiry as string
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "12", "-5") + "]}");       // negative expiry
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "12", "1.5") + "]}");      // fractional expiry
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "12", "true") + "]}");     // boolean expiry
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "\"soon\"", "9") + "]}");  // age as string
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "-1", "9") + "]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":7,\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":[\"Bash\"],\"expires_in_s\":9,\"nonce\":\"abcdefgh\"}]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"summary\":123,\"expires_in_s\":9,"
                  "\"nonce\":\"abcdefgh\"}]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"summary\":{\"a\":1},\"expires_in_s\":9,"
                  "\"nonce\":\"abcdefgh\"}]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":12345678}]}");
  expect_rejected("{\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"expires_in_s\":9,\"nonce\":null}]}");
}

static void test_identifier_rules() {
  const std::string long41(41, 'a');
  const std::string long40(40, 'a');
  TEST_ASSERT_TRUE(parses("{\"approvals\":[" + item(long40) + "]}"));
  expect_rejected("{\"approvals\":[" + item(long41) + "]}");
  expect_rejected("{\"approvals\":[" + item("") + "]}");
  expect_rejected("{\"approvals\":[" + item("appr/../x") + "]}");   // would change the request path
  expect_rejected("{\"approvals\":[" + item("a b") + "]}");
  expect_rejected("{\"approvals\":[" + item("a?b=1") + "]}");
  TEST_ASSERT_TRUE(parses("{\"approvals\":[" + item(kId, long40) + "]}"));
  expect_rejected("{\"approvals\":[" + item(kId, long41) + "]}");
  expect_rejected("{\"approvals\":[" + item(kId, "") + "]}");
  expect_rejected("{\"approvals\":[" + item(kId, "Ba sh") + "]}");
  TEST_ASSERT_TRUE(parses("{\"approvals\":[" + item(kId, "mcp__srv__do.it:now-1") + "]}"));  // MCP tool names
}

static void test_nonce_rules() {
  TEST_ASSERT_TRUE(parses("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", "abcdefgh") + "]}"));       // 8
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", "abcdefg") + "]}");                 // 7
  TEST_ASSERT_TRUE(parses("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", std::string(32, 'n')) + "]}"));
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", std::string(33, 'n')) + "]}");
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", "abcd+fgh/") + "]}");  // not url-safe
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", "abcd efgh") + "]}");
}

static void test_summary_is_display_text_cut_and_cleaned() {
  ApprovalPage p;
  std::string big(300, 'z');
  std::string j = "{\"approvals\":[" + item(kId, "Bash", big) + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT32(kSummaryCap - 1, std::strlen(p.items[0].summary));
  // control characters and non-ASCII bytes are shown as '?'
  j = "{\"approvals\":[" + item(kId, "Bash", "a\\u0001b\\u00e9c\\u007f") + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_STRING("a?b??c?", p.items[0].summary);
  // JSON escapes that are printable stay what they are
  j = "{\"approvals\":[" + item(kId, "Bash", "echo \\\"hi\\\" \\\\ done") + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_STRING("echo \"hi\" \\ done", p.items[0].summary);
}

static void test_expiry_is_clamped_not_trusted() {
  ApprovalPage p;
  std::string j = "{\"approvals\":[" + item(kId, "Bash", "x", "1", "4000000000") + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT32(kMaxExpiresS, p.items[0].expires_in_s);
  j = "{\"approvals\":[" + item(kId, "Bash", "x", "1", "0") + "]}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT32(0, p.items[0].expires_in_s);
}

static void test_unknown_and_nested_fields_are_ignored() {
  ApprovalPage p;
  const std::string j =
      "{\"junk\":[1,2,{\"a\":[[[]]]}],\"approvals\":[{\"approval_id\":\"a1\",\"tool\":\"Bash\",\"extra\":{\"x\":[true,null]},"
      "\"summary\":\"s\",\"expires_in_s\":5,\"nonce\":\"abcdefgh\",\"future\":\"field\"}],\"more\":{}}";
  TEST_ASSERT_TRUE(parse_approvals(j.data(), j.size(), p));
  TEST_ASSERT_EQUAL_UINT8(1, p.count);
  TEST_ASSERT_EQUAL_STRING("s", p.items[0].summary);
}

static void test_embedded_nul_escapes_cannot_hide_text() {
  // "\u0000" would end a C string early: the whole reply is refused instead of showing a shortened text
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "ls\\u0000 rm -rf /") + "]}");
  expect_rejected("{\"approvals\":[" + item("appr-1\\u0000zzz") + "]}");
  expect_rejected("{\"approvals\":[" + item(kId, "Ba\\u0000sh") + "]}");
  expect_rejected("{\"approvals\":[" + item(kId, "Bash", "x", "1", "9", "abcdefgh\\u0000ij") + "]}");
}

// ---- status -------------------------------------------------------------------------------------------------

static void test_status_not_configured() {
  Status s;
  const std::string j = "{\"configured\":false}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_FALSE(s.configured);
  TEST_ASSERT_TRUE(all_zero(s));
}

static void test_status_configured_full() {
  Status s;
  const std::string j =
      "{\"configured\":true,\"updated_s_ago\":7,\"model\":\"Opus\",\"cost_usd\":0.0123,\"context_pct\":8.0,"
      "\"five_hour_pct\":12.5,\"seven_day_pct\":3}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_TRUE(s.configured && s.has_age && s.has_cost && s.has_context && s.has_five_hour && s.has_seven_day);
  TEST_ASSERT_EQUAL_UINT32(7, s.updated_s_ago);
  TEST_ASSERT_EQUAL_STRING("Opus", s.model);
  TEST_ASSERT_FLOAT_WITHIN(0.0001f, 0.0123f, s.cost_usd);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 8.0f, s.context_pct);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 12.5f, s.five_hour_pct);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 3.0f, s.seven_day_pct);
}

static void test_status_nulls_and_missing_plan_numbers_are_not_present() {
  Status s;
  std::string j =
      "{\"configured\":true,\"updated_s_ago\":7,\"model\":\"Opus\",\"cost_usd\":0.5,\"context_pct\":null,"
      "\"five_hour_pct\":null,\"seven_day_pct\":null}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_TRUE(s.has_cost);
  TEST_ASSERT_FALSE(s.has_context || s.has_five_hour || s.has_seven_day);
  j = "{\"configured\":true,\"model\":\"Sonnet\"}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_FALSE(s.has_age || s.has_cost || s.has_context || s.has_five_hour || s.has_seven_day);
  TEST_ASSERT_EQUAL_STRING("Sonnet", s.model);
  j = "{\"configured\":true,\"model\":null}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_EQUAL_STRING("", s.model);
}

static void test_status_wrong_types_and_ranges_are_refused() {
  const char* bad[] = {
      "{}",
      "{\"configured\":\"yes\"}",
      "{\"configured\":1}",
      "{\"configured\":true,\"cost_usd\":\"1.5\"}",
      "{\"configured\":true,\"cost_usd\":true}",
      "{\"configured\":true,\"cost_usd\":-0.01}",
      "{\"configured\":true,\"cost_usd\":2000000}",
      "{\"configured\":true,\"context_pct\":101}",
      "{\"configured\":true,\"context_pct\":-1}",
      "{\"configured\":true,\"five_hour_pct\":\"12\"}",
      "{\"configured\":true,\"seven_day_pct\":[1]}",
      "{\"configured\":true,\"model\":7}",
      "{\"configured\":true,\"model\":[\"a\"]}",
      "{\"configured\":true,\"updated_s_ago\":-3}",
      "{\"configured\":true,\"updated_s_ago\":\"7\"}",
      "[]",
      "\"configured\"",
  };
  for (const char* j : bad) {
    Status s;
    TEST_ASSERT_FALSE_MESSAGE(parse_status(j, std::strlen(j), s), j);
    TEST_ASSERT_TRUE(all_zero(s));
  }
}

static void test_status_model_is_display_text() {
  Status s;
  std::string j = "{\"configured\":true,\"model\":\"" + std::string(200, 'm') + "\"}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_EQUAL_UINT32(kModelCap - 1, std::strlen(s.model));
  j = "{\"configured\":true,\"model\":\"Op\\u0001us\"}";
  TEST_ASSERT_TRUE(parse_status(j.data(), j.size(), s));
  TEST_ASSERT_EQUAL_STRING("Op?us", s.model);
}

// ---- decision -----------------------------------------------------------------------------------------------

static void test_decide_reply() {
  DecideReply r;
  std::string j = "{\"approval_id\":\"appr-0a1b2c3d4e5f6071\",\"state\":\"allowed\",\"decision\":\"allow\"}";
  TEST_ASSERT_TRUE(parse_decide_reply(j.data(), j.size(), r));
  TEST_ASSERT_TRUE(r.state == Decided::Allowed);
  TEST_ASSERT_EQUAL_STRING(kId, r.approval_id);
  j = "{\"approval_id\":\"appr-0a1b2c3d4e5f6071\",\"state\":\"denied\",\"decision\":\"deny\"}";
  TEST_ASSERT_TRUE(parse_decide_reply(j.data(), j.size(), r));
  TEST_ASSERT_TRUE(r.state == Decided::Denied);
  j = "{\"state\":\"denied\"}";
  TEST_ASSERT_TRUE(parse_decide_reply(j.data(), j.size(), r));
  TEST_ASSERT_EQUAL_STRING("", r.approval_id);

  const char* bad[] = {"{}", "{\"state\":\"pending\"}", "{\"state\":3}", "{\"state\":null}", "{\"state\":\"allowed\\u0000x\"}",
                       "{\"state\":\"ALLOWED\"}", "{\"state\":\"allowed\",\"approval_id\":\"a/b\"}", "[]", "<html>"};
  for (const char* b : bad) {
    TEST_ASSERT_FALSE_MESSAGE(parse_decide_reply(b, std::strlen(b), r), b);
    TEST_ASSERT_TRUE(all_zero(r));
  }
}

static void test_request_builders() {
  char path[96];
  TEST_ASSERT_TRUE(path_decide(path, sizeof(path), kId));
  TEST_ASSERT_EQUAL_STRING("/v1/claude/approvals/appr-0a1b2c3d4e5f6071/decide", path);
  TEST_ASSERT_FALSE(path_decide(path, sizeof(path), ""));
  TEST_ASSERT_FALSE(path_decide(path, sizeof(path), nullptr));
  TEST_ASSERT_FALSE(path_decide(path, sizeof(path), "a/b"));
  TEST_ASSERT_FALSE(path_decide(path, sizeof(path), "a?x"));
  TEST_ASSERT_FALSE(path_decide(path, sizeof(path), std::string(41, 'a').c_str()));
  TEST_ASSERT_FALSE(path_decide(path, 20, kId));  // buffer too small

  char body[96];
  TEST_ASSERT_TRUE(body_decide(body, sizeof(body), true, kNonce));
  TEST_ASSERT_EQUAL_STRING("{\"decision\":\"allow\",\"nonce\":\"Xk3pQ9rT2mVb7LwZcD4aYe\"}", body);
  TEST_ASSERT_TRUE(body_decide(body, sizeof(body), false, kNonce));
  TEST_ASSERT_EQUAL_STRING("{\"decision\":\"deny\",\"nonce\":\"Xk3pQ9rT2mVb7LwZcD4aYe\"}", body);
  TEST_ASSERT_FALSE(body_decide(body, sizeof(body), true, "short"));
  TEST_ASSERT_FALSE(body_decide(body, sizeof(body), true, "bad\"quote12345"));
  TEST_ASSERT_FALSE(body_decide(body, sizeof(body), true, nullptr));
  TEST_ASSERT_FALSE(body_decide(body, sizeof(body), true, std::string(33, 'n').c_str()));
  TEST_ASSERT_FALSE(body_decide(body, 20, true, kNonce));  // buffer too small: the writer reports it
}

void run_claude_parse_tests() {
  RUN_TEST(test_empty_list);
  RUN_TEST(test_one_item_is_read_field_by_field);
  RUN_TEST(test_five_items_keep_their_order_and_six_are_refused);
  RUN_TEST(test_missing_or_null_summary_is_empty_not_an_error);
  RUN_TEST(test_required_fields_are_required);
  RUN_TEST(test_wrong_types_are_refused);
  RUN_TEST(test_identifier_rules);
  RUN_TEST(test_nonce_rules);
  RUN_TEST(test_summary_is_display_text_cut_and_cleaned);
  RUN_TEST(test_expiry_is_clamped_not_trusted);
  RUN_TEST(test_unknown_and_nested_fields_are_ignored);
  RUN_TEST(test_embedded_nul_escapes_cannot_hide_text);
  RUN_TEST(test_status_not_configured);
  RUN_TEST(test_status_configured_full);
  RUN_TEST(test_status_nulls_and_missing_plan_numbers_are_not_present);
  RUN_TEST(test_status_wrong_types_and_ranges_are_refused);
  RUN_TEST(test_status_model_is_display_text);
  RUN_TEST(test_decide_reply);
  RUN_TEST(test_request_builders);
}
