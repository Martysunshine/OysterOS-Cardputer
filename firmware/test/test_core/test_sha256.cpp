// SHA-256 (NIST FIPS 180-4 examples) and HMAC-SHA256 (RFC 4231) known-answer tests.
#include <unity.h>

#include <cstring>

#include "oyster_core/sha256.h"
#include "test_common.h"

using oyster::Sha256;

static void digest_hex(const uint8_t* msg, size_t n, char out[65]) {
  Sha256 h;
  h.update(msg, n);
  uint8_t d[32];
  h.finish(d);
  Sha256::to_hex(d, out);
}

static void test_sha256_abc() {
  char hex[65];
  digest_hex(reinterpret_cast<const uint8_t*>("abc"), 3, hex);
  TEST_ASSERT_EQUAL_STRING("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", hex);
}

static void test_sha256_empty() {
  char hex[65];
  Sha256 h;
  uint8_t d[32];
  h.finish(d);
  Sha256::to_hex(d, hex);
  TEST_ASSERT_EQUAL_STRING("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", hex);
  // update(nullptr/0) must be a no-op
  Sha256 h2;
  h2.update(nullptr, 0);
  h2.update(reinterpret_cast<const uint8_t*>(""), 0);
  h2.finish(d);
  Sha256::to_hex(d, hex);
  TEST_ASSERT_EQUAL_STRING("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", hex);
}

static void test_sha256_448_bit_two_blocks() {
  const char* m = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";  // 56 bytes: padding spills
  TEST_ASSERT_EQUAL_UINT32(56, std::strlen(m));
  char hex[65];
  digest_hex(reinterpret_cast<const uint8_t*>(m), std::strlen(m), hex);
  TEST_ASSERT_EQUAL_STRING("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1", hex);
}

static void test_sha256_896_bit() {
  const char* m =
      "abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu";
  char hex[65];
  digest_hex(reinterpret_cast<const uint8_t*>(m), std::strlen(m), hex);
  TEST_ASSERT_EQUAL_STRING("cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1", hex);
}

static void test_sha256_million_a_streamed_odd_chunks() {
  static uint8_t buf[1000];
  std::memset(buf, 'a', sizeof(buf));
  const size_t sizes[] = {1, 3, 7, 13, 63, 64, 65, 127, 129, 999};
  Sha256 h;
  size_t done = 0;
  size_t k = 0;
  while (done < 1000000) {
    size_t n = sizes[k++ % (sizeof(sizes) / sizeof(sizes[0]))];
    if (n > 1000000 - done) n = 1000000 - done;
    h.update(buf, n);
    done += n;
  }
  TEST_ASSERT_TRUE(h.bytes_hashed() == 1000000u);
  uint8_t d[32];
  h.finish(d);
  char hex[65];
  Sha256::to_hex(d, hex);
  TEST_ASSERT_EQUAL_STRING("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", hex);
}

static void test_sha256_reset_and_reuse() {
  Sha256 h;
  h.update(reinterpret_cast<const uint8_t*>("garbage"), 7);
  h.reset();
  TEST_ASSERT_TRUE(h.bytes_hashed() == 0u);
  h.update(reinterpret_cast<const uint8_t*>("ab"), 2);
  h.update(reinterpret_cast<const uint8_t*>("c"), 1);
  uint8_t d[32];
  h.finish(d);
  char hex[65];
  Sha256::to_hex(d, hex);
  TEST_ASSERT_EQUAL_STRING("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", hex);
}

static void hmac_hex(const uint8_t* key, size_t klen, const char* msg, char out[65]) {
  uint8_t mac[32];
  oyster::hmac_sha256(key, klen, reinterpret_cast<const uint8_t*>(msg), std::strlen(msg), mac);
  Sha256::to_hex(mac, out);
}

static void test_hmac_rfc4231_case1() {
  uint8_t key[20];
  std::memset(key, 0x0b, sizeof(key));
  char hex[65];
  hmac_hex(key, sizeof(key), "Hi There", hex);
  TEST_ASSERT_EQUAL_STRING("b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7", hex);
}

static void test_hmac_rfc4231_case2() {
  char hex[65];
  hmac_hex(reinterpret_cast<const uint8_t*>("Jefe"), 4, "what do ya want for nothing?", hex);
  TEST_ASSERT_EQUAL_STRING("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", hex);
}

static void test_hmac_rfc4231_case6_long_key() {
  uint8_t key[131];
  std::memset(key, 0xaa, sizeof(key));
  char hex[65];
  hmac_hex(key, sizeof(key), "Test Using Larger Than Block-Size Key - Hash Key First", hex);
  TEST_ASSERT_EQUAL_STRING("60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54", hex);
}

static void test_equal_ct() {
  const uint8_t a[4] = {1, 2, 3, 4};
  const uint8_t b[4] = {1, 2, 3, 4};
  const uint8_t c[4] = {1, 2, 3, 5};
  TEST_ASSERT_TRUE(oyster::equal_ct(a, b, 4));
  TEST_ASSERT_FALSE(oyster::equal_ct(a, c, 4));
  TEST_ASSERT_TRUE(oyster::equal_ct(a, c, 3));
  TEST_ASSERT_TRUE(oyster::equal_ct(nullptr, nullptr, 0));
  TEST_ASSERT_FALSE(oyster::equal_ct(a, nullptr, 4));
}

void run_sha256_tests() {
  RUN_TEST(test_sha256_abc);
  RUN_TEST(test_sha256_empty);
  RUN_TEST(test_sha256_448_bit_two_blocks);
  RUN_TEST(test_sha256_896_bit);
  RUN_TEST(test_sha256_million_a_streamed_odd_chunks);
  RUN_TEST(test_sha256_reset_and_reuse);
  RUN_TEST(test_hmac_rfc4231_case1);
  RUN_TEST(test_hmac_rfc4231_case2);
  RUN_TEST(test_hmac_rfc4231_case6_long_key);
  RUN_TEST(test_equal_ct);
}
