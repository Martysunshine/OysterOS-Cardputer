// Host tests for the direct-AI wire format (oyster_core/directai_parse.h): the request-body builder
// and the reply parser. Run: pio test -e native -f test_directai
#include <unity.h>

#include <ArduinoJson.h>

#include <cstring>
#include <string>

#include "oyster_core/directai_parse.h"

using namespace oyster::directai;

namespace {

// Deserialize a produced body and return choices-less: here we just validate top-level request shape.
bool body_parses(const char* body, size_t len, std::string& model_out, std::string& sys_out,
                 std::string& user_out, long& max_tokens) {
  JsonDocument doc;
  if (deserializeJson(doc, body, len) != DeserializationError::Ok) return false;
  model_out = doc["model"].as<const char*>() ? doc["model"].as<const char*>() : "";
  JsonArrayConst msgs = doc["messages"].as<JsonArrayConst>();
  if (msgs.isNull() || msgs.size() != 2) return false;
  if (std::strcmp(msgs[0]["role"].as<const char*>(), "system") != 0) return false;
  if (std::strcmp(msgs[1]["role"].as<const char*>(), "user") != 0) return false;
  sys_out = msgs[0]["content"].as<const char*>() ? msgs[0]["content"].as<const char*>() : "";
  user_out = msgs[1]["content"].as<const char*>() ? msgs[1]["content"].as<const char*>() : "";
  max_tokens = doc["max_completion_tokens"].as<long>();
  return true;
}

std::string reply_with_content(const std::string& content) {
  return "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":" + content + "}}]}";
}

}  // namespace

// ---- build_chat_body ------------------------------------------------------------------------------

void test_build_normal() {
  char buf[2048];
  const size_t n = build_chat_body("llama-3.1-8b", "What is TLS?", buf, sizeof(buf));
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_EQUAL_size_t(std::strlen(buf), n);
  std::string model, sys, user;
  long mt = 0;
  TEST_ASSERT_TRUE(body_parses(buf, n, model, sys, user, mt));
  TEST_ASSERT_EQUAL_STRING("llama-3.1-8b", model.c_str());
  TEST_ASSERT_EQUAL_STRING("What is TLS?", user.c_str());
  TEST_ASSERT_TRUE(sys.find("no device tools") != std::string::npos);
  TEST_ASSERT_TRUE(mt > 0);
}

void test_build_escapes_quotes_and_newlines() {
  char buf[2048];
  const char* q = "He said \"hi\"\nand left\tnow";
  const size_t n = build_chat_body("m", q, buf, sizeof(buf));
  TEST_ASSERT_TRUE(n > 0);
  // The raw body must not contain a bare double-quote-newline; it must be valid JSON that round-trips.
  std::string model, sys, user;
  long mt = 0;
  TEST_ASSERT_TRUE(body_parses(buf, n, model, sys, user, mt));
  TEST_ASSERT_EQUAL_STRING(q, user.c_str());  // ASCII control + quotes round-trip exactly
  // No literal control byte (< 0x20) may appear in the serialized body.
  for (size_t i = 0; i < n; ++i) TEST_ASSERT_TRUE(static_cast<unsigned char>(buf[i]) >= 0x20);
}

void test_build_escapes_unicode_and_control() {
  char buf[2048];
  // UTF-8 (e-acute, euro sign) plus a raw control byte.
  const char* q = "caf\xC3\xA9 \xE2\x82\xAC \x01 end";
  const size_t n = build_chat_body("m", q, buf, sizeof(buf));
  TEST_ASSERT_TRUE(n > 0);
  JsonDocument doc;
  TEST_ASSERT_TRUE(deserializeJson(doc, buf, n) == DeserializationError::Ok);
  // Still a non-empty user content; no bare control bytes in the wire body.
  const char* uc = doc["messages"][1]["content"].as<const char*>();
  TEST_ASSERT_NOT_NULL(uc);
  TEST_ASSERT_TRUE(uc[0] != '\0');
  for (size_t i = 0; i < n; ++i) TEST_ASSERT_TRUE(static_cast<unsigned char>(buf[i]) >= 0x20);
}

void test_build_bounded() {
  char small[16];
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("model", "a long enough question", small, sizeof(small)));
  TEST_ASSERT_EQUAL_CHAR('\0', small[0]);
  // A huge question into a modest buffer: refused, buffer cleared, never overflowed.
  std::string big(4000, 'x');
  char buf[1024];
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("model", big.c_str(), buf, sizeof(buf)));
  TEST_ASSERT_EQUAL_CHAR('\0', buf[0]);
}

void test_build_rejects_null_and_empty() {
  char buf[256];
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body(nullptr, "q", buf, sizeof(buf)));
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("m", nullptr, buf, sizeof(buf)));
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("m", "", buf, sizeof(buf)));
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("", "q", buf, sizeof(buf)));
  TEST_ASSERT_EQUAL_size_t(0, build_chat_body("m", "q", nullptr, 100));
}

// ---- parse_chat_answer ----------------------------------------------------------------------------

void test_parse_normal() {
  char out[256];
  const std::string r = reply_with_content("\"Hello, world.\"");
  TEST_ASSERT_TRUE(parse_chat_answer(r.c_str(), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("Hello, world.", out);
}

void test_parse_missing_content() {
  char out[256];
  TEST_ASSERT_FALSE(parse_chat_answer("{\"choices\":[{\"message\":{}}]}", out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
}

void test_parse_no_choices_and_empty_array() {
  char out[256];
  TEST_ASSERT_FALSE(parse_chat_answer("{}", out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
  TEST_ASSERT_FALSE(parse_chat_answer("{\"choices\":[]}", out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
}

void test_parse_empty_content_string() {
  char out[256];
  TEST_ASSERT_FALSE(parse_chat_answer(reply_with_content("\"\"").c_str(), out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
}

void test_parse_error_object() {
  char out[256];
  const char* r = "{\"error\":{\"message\":\"Invalid API Key\",\"type\":\"auth\"}}";
  TEST_ASSERT_FALSE(parse_chat_answer(r, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("Invalid API Key", out);
}

void test_parse_error_bare_string() {
  char out[256];
  TEST_ASSERT_FALSE(parse_chat_answer("{\"error\":\"boom\"}", out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("boom", out);
}

void test_parse_non_json() {
  char out[256];
  TEST_ASSERT_FALSE(parse_chat_answer("not json at all {", out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
  TEST_ASSERT_FALSE(parse_chat_answer(nullptr, out, sizeof(out)));
  TEST_ASSERT_EQUAL_CHAR('\0', out[0]);
}

void test_parse_control_chars_folded() {
  char out[256];
  // content "a\tb\nc" with escaped control chars -> folded to spaces in the output.
  const std::string r = reply_with_content("\"a\\tb\\nc\"");
  TEST_ASSERT_TRUE(parse_chat_answer(r.c_str(), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("a b c", out);
}

void test_parse_truncation_is_bounded_and_utf8_safe() {
  // A long answer ending in multi-byte UTF-8, parsed into a small buffer.
  std::string content = "\"";
  content += std::string(40, 'A');
  content += "\xC3\xA9\xC3\xA9\xC3\xA9";  // e-acute x3
  content += "\"";
  const std::string r = reply_with_content(content);
  char out[32];
  const bool ok = parse_chat_answer(r.c_str(), out, sizeof(out));
  TEST_ASSERT_TRUE(ok);
  TEST_ASSERT_TRUE(std::strlen(out) < sizeof(out));
  // Must not end on a dangling UTF-8 lead byte (0xC0-0xFF) or continuation byte (0x80-0xBF).
  const size_t len = std::strlen(out);
  if (len > 0) {
    const unsigned char last = static_cast<unsigned char>(out[len - 1]);
    TEST_ASSERT_TRUE(last < 0x80);
  }
}

void test_parse_hostile_huge_and_nested() {
  char out[128];
  // Deeply nested arrays where choices should be.
  std::string nested = "{\"choices\":";
  nested += std::string(60, '[');
  nested += std::string(60, ']');
  nested += "}";
  TEST_ASSERT_FALSE(parse_chat_answer(nested.c_str(), out, sizeof(out)));
  // A huge valid content string: no crash, bounded output.
  std::string huge = reply_with_content("\"" + std::string(20000, 'Z') + "\"");
  const bool ok = parse_chat_answer(huge.c_str(), out, sizeof(out));
  if (ok) TEST_ASSERT_TRUE(std::strlen(out) < sizeof(out));
  // choices present but first element is not an object.
  TEST_ASSERT_FALSE(parse_chat_answer("{\"choices\":[42]}", out, sizeof(out)));
}

void test_parse_zero_cap_safe() {
  // cap 0 must not write anything.
  char out[1] = {'x'};
  TEST_ASSERT_FALSE(parse_chat_answer(reply_with_content("\"hi\"").c_str(), out, 0));
}

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_build_normal);
  RUN_TEST(test_build_escapes_quotes_and_newlines);
  RUN_TEST(test_build_escapes_unicode_and_control);
  RUN_TEST(test_build_bounded);
  RUN_TEST(test_build_rejects_null_and_empty);
  RUN_TEST(test_parse_normal);
  RUN_TEST(test_parse_missing_content);
  RUN_TEST(test_parse_no_choices_and_empty_array);
  RUN_TEST(test_parse_empty_content_string);
  RUN_TEST(test_parse_error_object);
  RUN_TEST(test_parse_error_bare_string);
  RUN_TEST(test_parse_non_json);
  RUN_TEST(test_parse_control_chars_folded);
  RUN_TEST(test_parse_truncation_is_bounded_and_utf8_safe);
  RUN_TEST(test_parse_hostile_huge_and_nested);
  RUN_TEST(test_parse_zero_cap_safe);
  return UNITY_END();
}
