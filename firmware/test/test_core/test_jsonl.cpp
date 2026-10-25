// JSONL recovery scan and field extraction (jsonl.h).
#include <unity.h>

#include <cstring>

#include "oyster_core/config.h"
#include "oyster_core/jsonl.h"
#include "test_common.h"

using namespace oyster;

static uint8_t g_scratch[cfg::kMaxEventBytes];

static JsonlScanResult scan_str(const char* s, size_t len) {
  MemoryByteSource src(reinterpret_cast<const uint8_t*>(s), len);
  return scan_jsonl(src, g_scratch, sizeof(g_scratch));
}

static const char kL1[] = "{\"schema_version\":\"1.0\",\"event_id\":\"evt-000001\",\"sequence\":1,\"details\":{}}\n";
static const char kL2[] = "{\"schema_version\":\"1.0\",\"event_id\":\"evt-000002\",\"sequence\":2,\"details\":{\"n\":1}}\n";
static const char kL3[] = "{\"schema_version\":\"1.0\",\"event_id\":\"evt-000003\",\"sequence\":3,\"details\":{}}\n";
static const char kPartial[] = "{\"schema_version\":\"1.0\",\"event_id\":\"evt-000004\",\"seq";

static void test_scan_empty_file() {
  JsonlScanResult r = scan_str("", 0);
  TEST_ASSERT_EQUAL_UINT32(0, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(0, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(0, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);
  TEST_ASSERT_FALSE(r.has_sequence);
  TEST_ASSERT_EQUAL_UINT32(0, r.last_sequence);
  TEST_ASSERT_FALSE(r.line_too_long);
}

static void test_scan_complete_records() {
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, kL2);
  std::strcat(buf, kL3);
  JsonlScanResult r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(0, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf), r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);
  TEST_ASSERT_TRUE(r.has_sequence);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(kL2), r.longest_line);  // newline included
}

static void test_scan_truncated_final_record_keeps_prior_records() {
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, kL2);
  std::strcat(buf, kL3);
  const size_t good = std::strlen(buf);
  std::strcat(buf, kPartial);
  JsonlScanResult r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(0, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(good, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(kPartial), r.truncated_tail);
  TEST_ASSERT_TRUE(r.has_sequence);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);

  // Recovery policy: append one '\n'; the partial record becomes an invalid line, nothing is lost.
  std::strcat(buf, "\n");
  r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(1, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf), r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);

  // Appending after recovery continues normally.
  std::strcat(buf, "{\"sequence\":4}\n");
  r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(4, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(4, r.last_sequence);
}

// A complete record that only lacks its '\n' counts as valid (its sequence must not be reused) but still
// needs the repair '\n'.
static void test_scan_complete_unterminated_final_record_is_valid() {
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, kL2);
  const size_t good = std::strlen(buf);
  const char* tail = "{\"schema_version\":\"1.0\",\"sequence\":3,\"details\":{\"x\":[1,2],\"note\":\"a}b{\"}}";
  std::strcat(buf, tail);
  JsonlScanResult r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(0, r.invalid_lines);
  TEST_ASSERT_TRUE(r.has_sequence);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(good, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(tail), r.truncated_tail);  // the repair step still appends '\n'

  // After the repair newline the same file scans clean.
  std::strcat(buf, "\n");
  r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(0, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf), r.last_good_end);

  // A file whose only record lacks the newline.
  const char* only = "{\"sequence\":1}";
  r = scan_str(only, std::strlen(only));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(1, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(0, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(only), r.truncated_tail);
}

static void test_scan_cut_off_final_record_is_not_valid_even_at_a_nested_brace() {
  // Cut right after a nested '}' and still starts with '{' / ends with '}' - but the object is not closed.
  const char* cut_nested = "{\"sequence\":3,\"details\":{\"x\":1}";
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, cut_nested);
  JsonlScanResult r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(1, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(cut_nested), r.truncated_tail);

  const char* tails[] = {
      "{\"details\":{\"x\":1}}",                    // balanced but no sequence
      "{\"sequence\":3,\"note\":\"x\\\"}",          // string never closes (escaped quote)
      "{\"sequence\":3}}",                          // extra closing brace
      "{\"sequence\":3}{\"sequence\":4}",           // two objects on one line
      "{\"sequence\":3,\"a\":[1,2}",                // bracket never closed
      "{\"sequence\":-3}",                          // not an unsigned integer
  };
  for (const char* t : tails) {
    std::strcpy(buf, kL1);
    std::strcat(buf, t);
    r = scan_str(buf, std::strlen(buf));
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1, r.valid_records, t);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1, r.last_sequence, t);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(std::strlen(t), r.truncated_tail, t);
  }
}

static void test_scan_truncated_zero_fill_tail() {
  // FAT can leave zero-filled bytes after a power loss.
  uint8_t buf[256];
  const size_t l1 = std::strlen(kL1);
  std::memcpy(buf, kL1, l1);
  std::memset(buf + l1, 0, 40);
  MemoryByteSource src(buf, l1 + 40);
  JsonlScanResult r = scan_jsonl(src, g_scratch, sizeof(g_scratch));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(l1, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(40, r.truncated_tail);
}

static void test_scan_invalid_middle_line_skipped() {
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, "this is not json\n");
  std::strcat(buf, "{\"no_sequence\":true}\n");
  std::strcat(buf, "{\"sequence\":\"7\"}\n");    // string, not an integer
  std::strcat(buf, "{\"sequence\":-7}\n");       // negative
  std::strcat(buf, "{\"sequence\":1.5}\n");      // not an integer
  std::strcat(buf, "{\"sequence\":5000000000}\n");  // exceeds uint32
  std::strcat(buf, "{\"sequence\":9\n");         // no closing brace
  std::strcat(buf, "\n");                        // empty line
  std::strcat(buf, kL3);
  JsonlScanResult r = scan_str(buf, std::strlen(buf));
  TEST_ASSERT_EQUAL_UINT32(2, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(8, r.invalid_lines);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf), r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(3, r.last_sequence);
}

static void test_scan_last_sequence_is_highest() {
  const char* s = "{\"sequence\":7}\n{\"sequence\":3}\n{\"details\":{\"sequence\":99},\"sequence\":5}\n";
  JsonlScanResult r = scan_str(s, std::strlen(s));
  TEST_ASSERT_EQUAL_UINT32(3, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(7, r.last_sequence);
}

static void test_scan_line_too_long() {
  // A line longer than the scratch buffer is counted invalid and skipped; later lines still count.
  char buf[512] = "{\"sequence\":1,\"pad\":\"";
  for (int i = 0; i < 100; ++i) std::strcat(buf, "x");
  std::strcat(buf, "\"}\n");
  const size_t long_len = std::strlen(buf);
  std::strcat(buf, "{\"sequence\":2}\n");
  uint8_t scratch[32];
  MemoryByteSource src(reinterpret_cast<const uint8_t*>(buf), std::strlen(buf));
  JsonlScanResult r = scan_jsonl(src, scratch, sizeof(scratch));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(1, r.invalid_lines);
  TEST_ASSERT_TRUE(r.line_too_long);
  TEST_ASSERT_EQUAL_UINT32(long_len, r.longest_line);
  TEST_ASSERT_EQUAL_UINT32(2, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(0, r.truncated_tail);

  // An over-long final line without a newline is a truncated tail.
  char buf2[256] = "{\"sequence\":1}\n{\"sequence\":2,\"pad\":\"";
  for (int i = 0; i < 60; ++i) std::strcat(buf2, "y");
  MemoryByteSource src2(reinterpret_cast<const uint8_t*>(buf2), std::strlen(buf2));
  r = scan_jsonl(src2, scratch, sizeof(scratch));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(15, r.last_good_end);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(buf2) - 15, r.truncated_tail);
}

// Returns at most 5 bytes per call, like a slow block device.
class TrickleSource : public ByteSource {
 public:
  TrickleSource(const uint8_t* d, size_t n) : inner_(d, n) {}
  size_t size() const override { return inner_.size(); }
  size_t read_at(size_t offset, uint8_t* buf, size_t n) override {
    return inner_.read_at(offset, buf, n < 5 ? n : 5);
  }

 private:
  MemoryByteSource inner_;
};

static void test_scan_tolerates_short_reads() {
  char buf[512] = "";
  std::strcat(buf, kL1);
  std::strcat(buf, kL2);
  std::strcat(buf, kPartial);
  TrickleSource src(reinterpret_cast<const uint8_t*>(buf), std::strlen(buf));
  JsonlScanResult r = scan_jsonl(src, g_scratch, sizeof(g_scratch));
  TEST_ASSERT_EQUAL_UINT32(2, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(2, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(kPartial), r.truncated_tail);
}

static void test_scan_unterminated_valid_tail_with_short_reads() {
  const char* only = "{\"sequence\":1,\"details\":{}}";
  TrickleSource trickle(reinterpret_cast<const uint8_t*>(only), std::strlen(only));
  JsonlScanResult r = scan_jsonl(trickle, g_scratch, sizeof(g_scratch));
  TEST_ASSERT_EQUAL_UINT32(1, r.valid_records);
  TEST_ASSERT_EQUAL_UINT32(1, r.last_sequence);
  TEST_ASSERT_EQUAL_UINT32(std::strlen(only), r.truncated_tail);
}

static void test_memory_byte_source_bounds() {
  const uint8_t d[4] = {1, 2, 3, 4};
  MemoryByteSource src(d, 4);
  uint8_t out[8] = {};
  TEST_ASSERT_EQUAL_UINT32(4, src.size());
  TEST_ASSERT_EQUAL_UINT32(2, src.read_at(2, out, 8));
  TEST_ASSERT_EQUAL_UINT8(3, out[0]);
  TEST_ASSERT_EQUAL_UINT32(0, src.read_at(4, out, 8));
  TEST_ASSERT_EQUAL_UINT32(0, src.read_at(100, out, 8));
}

static void test_extract_uint_field() {
  const char* s = "{\"event_id\":\"evt-000012\",\"sequence\":12,\"uptime_ms\" : 4294967296,\"z\":0}";
  uint64_t v = 0;
  TEST_ASSERT_TRUE(extract_uint_field(s, std::strlen(s), "sequence", v));
  TEST_ASSERT_TRUE(v == 12u);
  TEST_ASSERT_TRUE(extract_uint_field(s, std::strlen(s), "uptime_ms", v));
  TEST_ASSERT_TRUE(v == 4294967296ull);
  TEST_ASSERT_TRUE(extract_uint_field(s, std::strlen(s), "z", v));
  TEST_ASSERT_TRUE(v == 0u);
  TEST_ASSERT_FALSE(extract_uint_field(s, std::strlen(s), "missing", v));
  TEST_ASSERT_FALSE(extract_uint_field(s, std::strlen(s), "event_id", v));  // a string
  TEST_ASSERT_FALSE(extract_uint_field(s, std::strlen(s), nullptr, v));
  TEST_ASSERT_FALSE(extract_uint_field(nullptr, 10, "sequence", v));

  // A key-like text inside a string value is not a key.
  const char* t = "{\"note\":\"\\\"sequence\\\":7\",\"sequence\":8}";
  TEST_ASSERT_TRUE(extract_uint_field(t, std::strlen(t), "sequence", v));
  TEST_ASSERT_TRUE(v == 8u);
  const char* t2 = "{\"note\":\"sequence\",\"sequence\":9}";
  TEST_ASSERT_TRUE(extract_uint_field(t2, std::strlen(t2), "sequence", v));
  TEST_ASSERT_TRUE(v == 9u);

  // The shallowest occurrence wins.
  const char* n = "{\"details\":{\"sequence\":1},\"sequence\":2}";
  TEST_ASSERT_TRUE(extract_uint_field(n, std::strlen(n), "sequence", v));
  TEST_ASSERT_TRUE(v == 2u);

  const char* bad[] = {"{\"sequence\":-1}", "{\"sequence\":1.5}", "{\"sequence\":1e3}", "{\"sequence\":null}",
                       "{\"sequence\":}", "{\"sequence\":99999999999999999999}", "{\"sequence\":+1}"};
  for (const char* b : bad) {
    v = 77;
    TEST_ASSERT_FALSE_MESSAGE(extract_uint_field(b, std::strlen(b), "sequence", v), b);
    TEST_ASSERT_TRUE(v == 77u);  // untouched on failure
  }
  const char* max = "{\"sequence\":18446744073709551615}";
  TEST_ASSERT_TRUE(extract_uint_field(max, std::strlen(max), "sequence", v));
  TEST_ASSERT_TRUE(v == 18446744073709551615ull);

  // The length bound is honoured (no NUL required).
  const char* cut = "{\"sequence\":123}";
  TEST_ASSERT_TRUE(extract_uint_field(cut, 14, "sequence", v));  // sees "...:12"
  TEST_ASSERT_TRUE(v == 12u);
  TEST_ASSERT_FALSE(extract_uint_field(cut, 10, "sequence", v));  // key not complete
}

static void test_extract_str_field() {
  const char* s = "{\"event_id\":\"evt-000012\",\"session_id\" : \"sess-a\",\"esc\":\"a\\\"b\",\"n\":null,\"i\":3}";
  char out[16];
  TEST_ASSERT_TRUE(extract_str_field(s, std::strlen(s), "event_id", out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("evt-000012", out);
  TEST_ASSERT_TRUE(extract_str_field(s, std::strlen(s), "session_id", out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("sess-a", out);
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "esc", out, sizeof(out)));  // escapes not decoded
  TEST_ASSERT_EQUAL_STRING("", out);
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "n", out, sizeof(out)));
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "i", out, sizeof(out)));
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "missing", out, sizeof(out)));
  char small[10];
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "event_id", small, sizeof(small)));  // 10 chars + NUL
  TEST_ASSERT_EQUAL_STRING("", small);
  char exact[11];
  TEST_ASSERT_TRUE(extract_str_field(s, std::strlen(s), "event_id", exact, sizeof(exact)));
  TEST_ASSERT_FALSE(extract_str_field(s, std::strlen(s), "event_id", nullptr, 4));
  const char* empty = "{\"k\":\"\"}";
  TEST_ASSERT_TRUE(extract_str_field(empty, std::strlen(empty), "k", out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("", out);
  const char* unterminated = "{\"k\":\"abc";
  TEST_ASSERT_FALSE(extract_str_field(unterminated, std::strlen(unterminated), "k", out, sizeof(out)));
}

void run_jsonl_tests() {
  RUN_TEST(test_scan_empty_file);
  RUN_TEST(test_scan_complete_records);
  RUN_TEST(test_scan_truncated_final_record_keeps_prior_records);
  RUN_TEST(test_scan_complete_unterminated_final_record_is_valid);
  RUN_TEST(test_scan_cut_off_final_record_is_not_valid_even_at_a_nested_brace);
  RUN_TEST(test_scan_truncated_zero_fill_tail);
  RUN_TEST(test_scan_invalid_middle_line_skipped);
  RUN_TEST(test_scan_last_sequence_is_highest);
  RUN_TEST(test_scan_line_too_long);
  RUN_TEST(test_scan_tolerates_short_reads);
  RUN_TEST(test_scan_unterminated_valid_tail_with_short_reads);
  RUN_TEST(test_memory_byte_source_bounds);
  RUN_TEST(test_extract_uint_field);
  RUN_TEST(test_extract_str_field);
}
