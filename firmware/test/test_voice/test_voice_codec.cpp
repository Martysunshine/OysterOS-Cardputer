// Host tests for the voice question audio path (oyster_core/voice_codec.h). Run: pio test -e native -f test_voice
#include <unity.h>

#include <cstdint>
#include <cstring>

#include "oyster_core/voice_codec.h"

using namespace oyster::voice;

void setUp() {}
void tearDown() {}

// ---- mu-law --------------------------------------------------------------------------------------

void test_ulaw_reference_points() {
  // The G.711 anchors: silence is 0xFF, full scale 0x80 (positive) and 0x00 (negative).
  TEST_ASSERT_EQUAL_HEX8(0xFF, ulaw_encode(0));
  TEST_ASSERT_EQUAL_HEX8(0x80, ulaw_encode(32767));
  TEST_ASSERT_EQUAL_HEX8(0x80, ulaw_encode(32635));
  TEST_ASSERT_EQUAL_HEX8(0x00, ulaw_encode(-32768));
  TEST_ASSERT_EQUAL_HEX8(0x00, ulaw_encode(-32767));
  // -1 differs from 0 only by the sign bit.
  TEST_ASSERT_EQUAL_HEX8(0x7F, ulaw_encode(-1));
  TEST_ASSERT_EQUAL_INT16(0, ulaw_decode(0xFF));
  TEST_ASSERT_EQUAL_INT16(0, ulaw_decode(0x7F));
  TEST_ASSERT_EQUAL_INT16(32124, ulaw_decode(0x80));
  TEST_ASSERT_EQUAL_INT16(-32124, ulaw_decode(0x00));
}

void test_ulaw_round_trip_error_is_within_a_quantisation_step() {
  // The step grows with the magnitude: 2^(exponent+3) with exponent 0..7, so the error stays under 1/8 of
  // the magnitude (plus the bias) everywhere.
  for (int v = -32768; v <= 32767; v += 37) {
    const int16_t back = ulaw_decode(ulaw_encode(static_cast<int16_t>(v)));
    int mag = v < 0 ? -v : v;
    if (mag > 32635) mag = 32635;
    const int err = back > v ? back - v : v - back;
    const int limit = mag / 8 + 0x84 / 8 + 1 + (mag > 32124 ? mag - 32124 : 0);
    TEST_ASSERT_TRUE_MESSAGE(err <= limit, "round trip error exceeds a quantisation step");
    // The sign survives (zero may decode as zero either way).
    if (v > 0x84) TEST_ASSERT_TRUE(back > 0);
    if (v < -0x84) TEST_ASSERT_TRUE(back < 0);
  }
}

void test_ulaw_is_monotonic_in_each_sign() {
  // Decoded values never decrease as the input increases (a basic property of a companding law).
  int16_t prev = ulaw_decode(ulaw_encode(static_cast<int16_t>(-32768)));
  for (int v = -32768 + 97; v <= 32767; v += 97) {
    const int16_t cur = ulaw_decode(ulaw_encode(static_cast<int16_t>(v)));
    TEST_ASSERT_TRUE(cur >= prev);
    prev = cur;
  }
}

// ---- decimation and level ----------------------------------------------------------------------

void test_decimate_averages_pairs_and_reports_peak() {
  const int16_t in[8] = {100, 300, -1000, -3000, 20000, 20000, 7, 0};
  uint8_t out[4] = {0, 0, 0, 0};
  uint16_t peak = 0;
  TEST_ASSERT_EQUAL_UINT(4, decimate2_ulaw(in, 8, out, &peak));
  TEST_ASSERT_EQUAL_UINT16(20000, peak);
  TEST_ASSERT_EQUAL_HEX8(ulaw_encode(200), out[0]);
  TEST_ASSERT_EQUAL_HEX8(ulaw_encode(-2000), out[1]);
  TEST_ASSERT_EQUAL_HEX8(ulaw_encode(20000), out[2]);
  TEST_ASSERT_EQUAL_HEX8(ulaw_encode(3), out[3]);
}

void test_decimate_drops_odd_trailing_sample_and_handles_empty() {
  const int16_t in[3] = {10, 20, 30000};
  uint8_t out[2] = {0xAA, 0xAA};
  uint16_t peak = 1;
  TEST_ASSERT_EQUAL_UINT(1, decimate2_ulaw(in, 3, out, &peak));
  TEST_ASSERT_EQUAL_HEX8(0xAA, out[1]);  // untouched
  TEST_ASSERT_EQUAL_UINT16(20, peak);    // the dropped sample does not count
  TEST_ASSERT_EQUAL_UINT(0, decimate2_ulaw(in, 0, out, &peak));
  TEST_ASSERT_EQUAL_UINT16(0, peak);
  TEST_ASSERT_EQUAL_UINT(0, decimate2_ulaw(in, 1, out, nullptr));
}

void test_decimate_peak_of_min_int16_is_clamped() {
  const int16_t in[2] = {-32768, -32768};
  uint8_t out[1];
  uint16_t peak = 0;
  decimate2_ulaw(in, 2, out, &peak);
  TEST_ASSERT_EQUAL_UINT16(32767, peak);
  TEST_ASSERT_EQUAL_HEX8(0x00, out[0]);
}

void test_level_percent_is_coarse_and_monotonic() {
  TEST_ASSERT_EQUAL_UINT8(0, level_percent(0));
  TEST_ASSERT_EQUAL_UINT8(6, level_percent(1));
  TEST_ASSERT_EQUAL_UINT8(100, level_percent(32767));
  TEST_ASSERT_EQUAL_UINT8(100, level_percent(16384));
  TEST_ASSERT_EQUAL_UINT8(93, level_percent(16383));
  uint8_t prev = 0;
  for (uint32_t p = 1; p <= 32767; p = p * 2 + 1) {
    const uint8_t cur = level_percent(static_cast<uint16_t>(p));
    TEST_ASSERT_TRUE(cur >= prev);
    prev = cur;
  }
}

// ---- ring ----------------------------------------------------------------------------------------

void test_ring_push_pop_wraps_and_keeps_order() {
  uint8_t storage[8];
  ByteRing r;
  r.reset(storage, sizeof(storage));
  TEST_ASSERT_EQUAL_UINT(0, r.available());
  TEST_ASSERT_EQUAL_UINT(7, r.free_space());
  const uint8_t a[5] = {1, 2, 3, 4, 5};
  TEST_ASSERT_TRUE(r.push(a, 5));
  TEST_ASSERT_EQUAL_UINT(5, r.available());
  TEST_ASSERT_FALSE(r.push(a, 3));  // only 2 fit: all or nothing
  TEST_ASSERT_EQUAL_UINT(5, r.available());
  uint8_t out[8] = {0};
  TEST_ASSERT_EQUAL_UINT(3, r.pop(out, 3));
  TEST_ASSERT_EQUAL_UINT8(1, out[0]);
  TEST_ASSERT_EQUAL_UINT8(3, out[2]);
  const uint8_t b[4] = {6, 7, 8, 9};
  TEST_ASSERT_TRUE(r.push(b, 4));  // wraps around the end of the storage
  TEST_ASSERT_EQUAL_UINT(6, r.available());
  TEST_ASSERT_EQUAL_UINT(6, r.pop(out, 8));
  const uint8_t expect[6] = {4, 5, 6, 7, 8, 9};
  TEST_ASSERT_EQUAL_UINT8_ARRAY(expect, out, 6);
  TEST_ASSERT_EQUAL_UINT(0, r.pop(out, 8));
}

void test_ring_without_storage_is_inert() {
  ByteRing r;
  const uint8_t a[1] = {1};
  uint8_t out[1];
  TEST_ASSERT_FALSE(r.push(a, 1));
  TEST_ASSERT_TRUE(r.push(a, 0));
  TEST_ASSERT_EQUAL_UINT(0, r.pop(out, 1));
  TEST_ASSERT_EQUAL_UINT(0, r.available());
  TEST_ASSERT_EQUAL_UINT(0, r.free_space());
}

void test_ring_many_rounds_never_lose_bytes() {
  uint8_t storage[13];
  ByteRing r;
  r.reset(storage, sizeof(storage));
  uint8_t next_in = 0, next_out = 0;
  for (int round = 0; round < 200; ++round) {
    uint8_t chunk[5];
    const size_t n = static_cast<size_t>(round % 5) + 1;
    for (size_t i = 0; i < n; ++i) chunk[i] = next_in++;
    if (!r.push(chunk, n)) {
      next_in = static_cast<uint8_t>(next_in - n);  // not accepted: nothing was written
    }
    uint8_t out[4];
    const size_t got = r.pop(out, static_cast<size_t>(round % 4) + 1);
    for (size_t i = 0; i < got; ++i) TEST_ASSERT_EQUAL_UINT8(next_out++, out[i]);
  }
  uint8_t rest[16];
  const size_t got = r.pop(rest, sizeof(rest));
  for (size_t i = 0; i < got; ++i) TEST_ASSERT_EQUAL_UINT8(next_out++, rest[i]);
  TEST_ASSERT_EQUAL_UINT8(next_in, next_out);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_ulaw_reference_points);
  RUN_TEST(test_ulaw_round_trip_error_is_within_a_quantisation_step);
  RUN_TEST(test_ulaw_is_monotonic_in_each_sign);
  RUN_TEST(test_decimate_averages_pairs_and_reports_peak);
  RUN_TEST(test_decimate_drops_odd_trailing_sample_and_handles_empty);
  RUN_TEST(test_decimate_peak_of_min_int16_is_clamped);
  RUN_TEST(test_level_percent_is_coarse_and_monotonic);
  RUN_TEST(test_ring_push_pop_wraps_and_keeps_order);
  RUN_TEST(test_ring_without_storage_is_inert);
  RUN_TEST(test_ring_many_rounds_never_lose_bytes);
  return UNITY_END();
}
