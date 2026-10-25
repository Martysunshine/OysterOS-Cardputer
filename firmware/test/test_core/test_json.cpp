// JsonWriter encoding/bounds and the display/format helpers (json_writer.h).
#include <unity.h>

#include <cstring>

#include "oyster_core/json_writer.h"
#include "test_common.h"

using namespace oyster;

static void test_json_object_members() {
  char buf[256];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  w.str("s", "x");
  w.num("n", -5);
  w.unum("u", 18446744073709551615ull);
  w.boolean("t", true);
  w.boolean("f", false);
  w.null("z");
  w.str("np", nullptr);
  w.raw("r", "{\"k\":1}");
  w.raw("rn", nullptr);
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING(
      "{\"s\":\"x\",\"n\":-5,\"u\":18446744073709551615,\"t\":true,\"f\":false,\"z\":null,\"np\":null,"
      "\"r\":{\"k\":1},\"rn\":null}",
      w.c_str());
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf), w.size());
}

static void test_json_nested_arrays_and_values() {
  char buf[256];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  w.begin_array("a");
  w.num_value(1);
  w.str_value("two");
  w.str_value(nullptr);
  w.raw_value("true");
  const uint8_t b[] = {0x00, 0xAB, 0xff};
  w.hex_value(b, sizeof(b));
  w.begin_object();
  w.num("k", 0);
  w.end_object();
  w.begin_array();
  w.end_array();
  w.end_array();
  w.begin_object("o");
  w.hex("h", b, sizeof(b));
  w.hex("e", b, 0);
  w.end_object();
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING("{\"a\":[1,\"two\",null,true,\"00abff\",{\"k\":0},[]],\"o\":{\"h\":\"00abff\",\"e\":\"\"}}",
                           w.c_str());
}

static void test_json_int64_extremes() {
  char buf[128];
  JsonWriter w(buf, sizeof(buf));
  w.begin_array();
  w.num_value(INT64_MIN);
  w.num_value(INT64_MAX);
  w.num_value(0);
  w.end_array();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING("[-9223372036854775808,9223372036854775807,0]", w.c_str());
}

static void test_json_escaping() {
  char buf[256];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  // quote, backslash, newline, tab, CR, BS, FF, 0x01, 0x1f, DEL, UTF-8 e-acute (2 bytes), 0xff
  w.str("k", "q\"b\\n\nt\tr\rb\bf\fc\x01" "d\x1f" "e\x7f" "\xc3\xa9\xff");
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING(
      "{\"k\":\"q\\\"b\\\\n\\nt\\tr\\rb\\bf\\fc\\u0001d\\u001fe\\u007f\\u00c3\\u00a9\\u00ff\"}", w.c_str());
}

static void test_json_str_bytes_raw() {
  char buf[128];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  const uint8_t ssid[] = {'O', 0x00, '"', 0x80};
  w.str_bytes("ssid", ssid, sizeof(ssid));
  w.str_bytes("none", nullptr, 3);
  w.str_bytes("empty", ssid, 0);
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING("{\"ssid\":\"O\\u0000\\\"\\u0080\",\"none\":null,\"empty\":\"\"}", w.c_str());
}

static void test_json_keys_are_escaped() {
  char buf[64];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  w.num("a\"b", 1);
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING("{\"a\\\"b\":1}", w.c_str());
}

static void test_json_tribool() {
  char buf[128];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  w.tribool("u", TriBool::unknown());
  w.tribool("d", TriBool{});
  w.tribool("t", TriBool::of(true));
  w.tribool("f", TriBool::of(false));
  w.end_object();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_STRING("{\"u\":null,\"d\":null,\"t\":true,\"f\":false}", w.c_str());
}

static void test_json_overflow_never_writes_past_buffer() {
  char mem[64];
  std::memset(mem, 'Z', sizeof(mem));
  const size_t cap = 16;
  JsonWriter w(mem, cap);
  w.begin_object();
  w.str("key", "a value that is much longer than sixteen bytes");
  w.end_object();
  TEST_ASSERT_FALSE(w.ok());
  TEST_ASSERT_TRUE(w.size() < cap);
  TEST_ASSERT_EQUAL_CHAR('\0', mem[w.size()]);
  TEST_ASSERT_TRUE(std::strlen(mem) < cap);
  for (size_t i = cap; i < sizeof(mem); ++i) TEST_ASSERT_EQUAL_CHAR('Z', mem[i]);
  // Once failed, it stays failed.
  w.num("x", 1);
  TEST_ASSERT_FALSE(w.ok());
  for (size_t i = cap; i < sizeof(mem); ++i) TEST_ASSERT_EQUAL_CHAR('Z', mem[i]);
}

static void test_json_overflow_exact_boundary() {
  // "{\"a\":1}" is 7 bytes; it needs a capacity of 8 including the terminator.
  char fit[8];
  JsonWriter w1(fit, sizeof(fit));
  w1.begin_object();
  w1.num("a", 1);
  w1.end_object();
  TEST_ASSERT_TRUE(w1.ok());
  TEST_ASSERT_EQUAL_STRING("{\"a\":1}", fit);

  char mem[16];
  std::memset(mem, 'Z', sizeof(mem));
  JsonWriter w2(mem, 7);
  w2.begin_object();
  w2.num("a", 1);
  w2.end_object();
  TEST_ASSERT_FALSE(w2.ok());
  for (size_t i = 7; i < sizeof(mem); ++i) TEST_ASSERT_EQUAL_CHAR('Z', mem[i]);

  // An escape sequence that does not fit also fails cleanly.
  std::memset(mem, 'Z', sizeof(mem));
  JsonWriter w3(mem, 10);
  w3.begin_object();
  w3.str("k", "\x01");  // {"k":"\u0001"} = 14 bytes
  TEST_ASSERT_FALSE(w3.ok());
  for (size_t i = 10; i < sizeof(mem); ++i) TEST_ASSERT_EQUAL_CHAR('Z', mem[i]);

  JsonWriter w4(nullptr, 10);
  TEST_ASSERT_FALSE(w4.ok());
  w4.begin_object();
  TEST_ASSERT_FALSE(w4.ok());
  char one[1] = {'Z'};
  JsonWriter w5(one, 0);
  w5.begin_object();
  TEST_ASSERT_FALSE(w5.ok());
  TEST_ASSERT_EQUAL_CHAR('Z', one[0]);
}

static void test_json_misuse_sets_not_ok() {
  char buf[128];
  JsonWriter w(buf, sizeof(buf));
  w.end_object();  // nothing open
  TEST_ASSERT_FALSE(w.ok());

  JsonWriter w2(buf, sizeof(buf));
  for (int i = 0; i < 15; ++i) w2.begin_array();
  TEST_ASSERT_TRUE(w2.ok());
  w2.begin_array();  // 16th level exceeds the nesting bound
  TEST_ASSERT_FALSE(w2.ok());

  JsonWriter w3(buf, sizeof(buf));
  w3.begin_object();
  w3.num(nullptr, 1);  // keys are mandatory
  TEST_ASSERT_FALSE(w3.ok());
}

static void test_display_safe() {
  char out[40];
  const uint8_t ok[] = {'O', 'y', 's', 't', 'e', 'r', '2'};
  TEST_ASSERT_TRUE(display_safe(ok, sizeof(ok), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("Oyster2", out);
  const uint8_t edges[] = {0x20, 0x7E};
  TEST_ASSERT_TRUE(display_safe(edges, sizeof(edges), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING(" ~", out);

  const uint8_t ctl[] = {'a', 0x07, 'b'};
  TEST_ASSERT_FALSE(display_safe(ctl, sizeof(ctl), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("", out);
  const uint8_t hi[] = {'a', 0x80};
  TEST_ASSERT_FALSE(display_safe(hi, sizeof(hi), out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("", out);
  const uint8_t del[] = {0x7F};
  TEST_ASSERT_FALSE(display_safe(del, sizeof(del), out, sizeof(out)));
  const uint8_t nul[] = {'a', 0x00, 'b'};
  TEST_ASSERT_FALSE(display_safe(nul, sizeof(nul), out, sizeof(out)));
  // Empty input has no display form (hidden SSID -> null).
  TEST_ASSERT_FALSE(display_safe(ok, 0, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("", out);
  // Does not fit: rejected rather than truncated.
  char small[7];
  TEST_ASSERT_FALSE(display_safe(ok, sizeof(ok), small, sizeof(small)));
  TEST_ASSERT_EQUAL_STRING("", small);
  char exact[8];
  TEST_ASSERT_TRUE(display_safe(ok, sizeof(ok), exact, sizeof(exact)));
  TEST_ASSERT_FALSE(display_safe(nullptr, 3, out, sizeof(out)));
}

static void test_format_mac() {
  const uint8_t mac[6] = {0x02, 0x00, 0xAB, 0xcd, 0xEF, 0x01};
  char out[18];
  format_mac(mac, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("02:00:ab:cd:ef:01", out);
  char small[17] = "untouched";
  format_mac(mac, small, sizeof(small));
  TEST_ASSERT_EQUAL_STRING("", small);
}

static void test_uuid_round_trip() {
  const char* s = "862f20d9-fa73-5f89-b130-86ee93e78261";
  uint8_t u[16];
  TEST_ASSERT_TRUE(parse_uuid128(s, u));
  TEST_ASSERT_EQUAL_HEX8(0x86, u[0]);
  TEST_ASSERT_EQUAL_HEX8(0x2f, u[1]);
  TEST_ASSERT_EQUAL_HEX8(0x61, u[15]);
  char out[37];
  format_uuid128(u, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING(s, out);

  // Case-insensitive parse, canonical lowercase output.
  TEST_ASSERT_TRUE(parse_uuid128("9F049067-D6CE-5453-9FF9-4DBB2B3342D4", u));
  format_uuid128(u, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("9f049067-d6ce-5453-9ff9-4dbb2b3342d4", out);

  // Every byte value survives a round trip.
  uint8_t all[16];
  for (int i = 0; i < 16; ++i) all[i] = static_cast<uint8_t>(i * 17);
  format_uuid128(all, out, sizeof(out));
  uint8_t back[16];
  TEST_ASSERT_TRUE(parse_uuid128(out, back));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(all, back, 16);

  char small[36] = "untouched";
  format_uuid128(u, small, sizeof(small));
  TEST_ASSERT_EQUAL_STRING("", small);
}

static void test_uuid_parse_rejects_bad_input() {
  uint8_t u[16];
  std::memset(u, 0x5A, sizeof(u));
  TEST_ASSERT_FALSE(parse_uuid128("862f20d9fa73-5f89-b130-86ee93e78261-", u));
  TEST_ASSERT_FALSE(parse_uuid128("862f20d9-fa73-5f89-b130-86ee93e7826", u));    // short
  TEST_ASSERT_FALSE(parse_uuid128("862f20d9-fa73-5f89-b130-86ee93e782611", u));  // long
  TEST_ASSERT_FALSE(parse_uuid128("862f20d9-fa73-5f89-b130-86ee93e7826g", u));   // non-hex
  TEST_ASSERT_FALSE(parse_uuid128("862f20d9_fa73-5f89-b130-86ee93e78261", u));   // wrong separator
  TEST_ASSERT_FALSE(parse_uuid128("{62f20d9-fa73-5f89-b130-86ee93e78261", u));
  TEST_ASSERT_FALSE(parse_uuid128("", u));
  TEST_ASSERT_FALSE(parse_uuid128(nullptr, u));
  TEST_ASSERT_FALSE(parse_uuid128("2a00", u));
  for (int i = 0; i < 16; ++i) TEST_ASSERT_EQUAL_HEX8(0x5A, u[i]);  // untouched on failure
}

void run_json_tests() {
  RUN_TEST(test_json_object_members);
  RUN_TEST(test_json_nested_arrays_and_values);
  RUN_TEST(test_json_int64_extremes);
  RUN_TEST(test_json_escaping);
  RUN_TEST(test_json_str_bytes_raw);
  RUN_TEST(test_json_keys_are_escaped);
  RUN_TEST(test_json_tribool);
  RUN_TEST(test_json_overflow_never_writes_past_buffer);
  RUN_TEST(test_json_overflow_exact_boundary);
  RUN_TEST(test_json_misuse_sets_not_ok);
  RUN_TEST(test_display_safe);
  RUN_TEST(test_format_mac);
  RUN_TEST(test_uuid_round_trip);
  RUN_TEST(test_uuid_parse_rejects_bad_input);
}
