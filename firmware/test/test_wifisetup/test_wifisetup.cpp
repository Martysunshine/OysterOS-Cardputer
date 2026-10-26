// Host tests for the Wi-Fi setup module's pure selection logic (oyster_core/wifi_pick.h):
// dedupe by SSID keeping the strongest RSSI, RSSI-descending sort, bounds/overflow, hidden-network
// skipping, open-network marking, and display-safe SSID sanitising. Run: pio test -e native -f test_wifisetup
#include <unity.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "oyster_core/wifi_pick.h"

using namespace oyster;
using oyster::wifi_pick::Pick;
using oyster::wifi_pick::PickList;

void setUp() {}
void tearDown() {}

namespace {

// Build an ApObservation with an ASCII SSID.
ApObservation ap(const char* ssid, int8_t rssi, WifiAuth auth = WifiAuth::Wpa2Psk) {
  ApObservation o{};
  const size_t len = std::strlen(ssid);
  o.ssid_len = static_cast<uint8_t>(len > 32 ? 32 : len);
  std::memcpy(o.ssid, ssid, o.ssid_len);
  o.rssi_dbm = rssi;
  o.auth = auth;
  o.channel = 6;
  return o;
}

std::string disp(const uint8_t* s, uint8_t len) {
  char buf[64];
  wifi_pick::display_ssid(s, len, buf, sizeof(buf));
  return std::string(buf);
}

std::string row(const Pick& p, size_t cols) {
  char buf[64];
  wifi_pick::format_row(p, buf, sizeof(buf), cols);
  return std::string(buf);
}

}  // namespace

// ---- build: basic ordering and fields -----------------------------------------------------------------

void test_sort_by_rssi_descending() {
  ApObservation in[] = {ap("weak", -80), ap("strong", -40), ap("mid", -60)};
  PickList out{};
  wifi_pick::build(in, 3, out);
  TEST_ASSERT_EQUAL_UINT8(3, out.count);
  TEST_ASSERT_EQUAL_STRING("strong", reinterpret_cast<const char*>(out.items[0].ssid));
  TEST_ASSERT_EQUAL_STRING("mid", reinterpret_cast<const char*>(out.items[1].ssid));
  TEST_ASSERT_EQUAL_STRING("weak", reinterpret_cast<const char*>(out.items[2].ssid));
  TEST_ASSERT_FALSE(out.overflow);
  TEST_ASSERT_EQUAL_UINT16(0, out.merged);
  TEST_ASSERT_EQUAL_UINT16(0, out.skipped_hidden);
}

void test_empty_input() {
  PickList out{};
  wifi_pick::build(nullptr, 0, out);
  TEST_ASSERT_EQUAL_UINT8(0, out.count);
  ApObservation none{};
  wifi_pick::build(&none, 0, out);
  TEST_ASSERT_EQUAL_UINT8(0, out.count);
}

// ---- build: dedupe keeps strongest ---------------------------------------------------------------------

void test_dedupe_keeps_strongest_rssi() {
  // Same SSID on two BSSIDs; the stronger sighting wins and both are reported as one entry.
  ApObservation in[] = {ap("home", -70, WifiAuth::Wpa2Psk), ap("home", -45, WifiAuth::Wpa3Psk),
                        ap("other", -55)};
  PickList out{};
  wifi_pick::build(in, 3, out);
  TEST_ASSERT_EQUAL_UINT8(2, out.count);
  TEST_ASSERT_EQUAL_UINT16(1, out.merged);
  // "home" at -45 is now the strongest overall and carries the stronger sighting's auth.
  TEST_ASSERT_EQUAL_STRING("home", reinterpret_cast<const char*>(out.items[0].ssid));
  TEST_ASSERT_EQUAL_INT(-45, out.items[0].rssi_dbm);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(WifiAuth::Wpa3Psk), static_cast<int>(out.items[0].auth));
}

void test_dedupe_weaker_second_sighting_ignored() {
  ApObservation in[] = {ap("home", -45), ap("home", -70)};
  PickList out{};
  wifi_pick::build(in, 2, out);
  TEST_ASSERT_EQUAL_UINT8(1, out.count);
  TEST_ASSERT_EQUAL_INT(-45, out.items[0].rssi_dbm);
  TEST_ASSERT_EQUAL_UINT16(1, out.merged);
}

// ---- build: hidden skipped -----------------------------------------------------------------------------

void test_hidden_networks_skipped() {
  ApObservation hidden{};  // ssid_len 0
  hidden.rssi_dbm = -30;
  hidden.auth = WifiAuth::Wpa2Psk;
  ApObservation in[] = {hidden, ap("visible", -60)};
  PickList out{};
  wifi_pick::build(in, 2, out);
  TEST_ASSERT_EQUAL_UINT8(1, out.count);
  TEST_ASSERT_EQUAL_UINT16(1, out.skipped_hidden);
  TEST_ASSERT_EQUAL_STRING("visible", reinterpret_cast<const char*>(out.items[0].ssid));
}

// ---- build: open flag ----------------------------------------------------------------------------------

void test_open_flag() {
  ApObservation in[] = {ap("cafe", -50, WifiAuth::Open), ap("secure", -55, WifiAuth::Wpa2Psk),
                        ap("wepnet", -60, WifiAuth::Wep)};
  PickList out{};
  wifi_pick::build(in, 3, out);
  TEST_ASSERT_EQUAL_UINT8(3, out.count);
  TEST_ASSERT_TRUE(out.items[0].open);    // cafe (open)
  TEST_ASSERT_FALSE(out.items[1].open);   // secure
  TEST_ASSERT_FALSE(out.items[2].open);   // WEP is not "open" (a key is required)
}

// ---- build: bounds / overflow --------------------------------------------------------------------------

void test_overflow_keeps_strongest() {
  // kMaxPicks distinct networks plus one stronger extra: the extra replaces the weakest, overflow set.
  const size_t cap = wifi_pick::kMaxPicks;
  ApObservation in[wifi_pick::kMaxPicks + 1];
  char name[16];
  for (size_t i = 0; i < cap; ++i) {
    std::snprintf(name, sizeof(name), "net%02u", static_cast<unsigned>(i));
    in[i] = ap(name, static_cast<int8_t>(-40 - static_cast<int>(i)));  // net00 strongest .. weaker
  }
  // The weakest so far is net(cap-1) at -(40+cap-1); add a stronger newcomer.
  in[cap] = ap("newcomer", -41);
  PickList out{};
  wifi_pick::build(in, cap + 1, out);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(cap), out.count);
  TEST_ASSERT_TRUE(out.overflow);
  // The weakest original (net at the bottom) must have been dropped; newcomer must be present.
  bool has_newcomer = false;
  for (size_t i = 0; i < out.count; ++i) {
    if (std::strcmp(reinterpret_cast<const char*>(out.items[i].ssid), "newcomer") == 0) has_newcomer = true;
  }
  TEST_ASSERT_TRUE(has_newcomer);
}

void test_overflow_weaker_newcomer_dropped() {
  const size_t cap = wifi_pick::kMaxPicks;
  ApObservation in[wifi_pick::kMaxPicks + 1];
  char name[16];
  for (size_t i = 0; i < cap; ++i) {
    std::snprintf(name, sizeof(name), "net%02u", static_cast<unsigned>(i));
    in[i] = ap(name, static_cast<int8_t>(-40 - static_cast<int>(i)));
  }
  in[cap] = ap("tooweak", -120);  // weaker than every kept network
  PickList out{};
  wifi_pick::build(in, cap + 1, out);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(cap), out.count);
  TEST_ASSERT_TRUE(out.overflow);
  for (size_t i = 0; i < out.count; ++i) {
    TEST_ASSERT_TRUE(std::strcmp(reinterpret_cast<const char*>(out.items[i].ssid), "tooweak") != 0);
  }
}

// ---- display sanitising --------------------------------------------------------------------------------

void test_display_hidden() {
  TEST_ASSERT_EQUAL_STRING("(hidden)", disp(nullptr, 0).c_str());
  const uint8_t empty[1] = {0};
  TEST_ASSERT_EQUAL_STRING("(hidden)", disp(empty, 0).c_str());
}

void test_display_sanitises_nonprintable() {
  const uint8_t ssid[] = {'C', 'a', 'f', 0x07, 0xC3, 0xA9, '!'};  // bell, UTF-8 e-acute, '!'
  // ssid_len counts all 7 bytes; the three non-ASCII become '?'.
  TEST_ASSERT_EQUAL_STRING("Caf?\?\?!", disp(ssid, 7).c_str());  // escaped to avoid the ??! trigraph
}

void test_display_truncates_to_buffer() {
  const uint8_t ssid[] = {'A', 'B', 'C', 'D', 'E'};
  char buf[4];
  wifi_pick::display_ssid(ssid, 5, buf, sizeof(buf));
  TEST_ASSERT_EQUAL_STRING("ABC", buf);  // n-1 = 3 characters
}

void test_display_full_32_byte_ssid() {
  uint8_t ssid[32];
  std::memset(ssid, 'Z', sizeof(ssid));
  std::string s = disp(ssid, 32);
  TEST_ASSERT_EQUAL_UINT(32, s.size());
}

// ---- row formatting ------------------------------------------------------------------------------------

void test_row_marks_open() {
  Pick p{};
  std::memcpy(p.ssid, "cafe", 4);
  p.ssid_len = 4;
  p.open = true;
  TEST_ASSERT_EQUAL_STRING("cafe (open)", row(p, 26).c_str());
}

void test_row_secured_has_no_marker() {
  Pick p{};
  std::memcpy(p.ssid, "home", 4);
  p.ssid_len = 4;
  p.open = false;
  TEST_ASSERT_EQUAL_STRING("home", row(p, 26).c_str());
}

void test_row_truncates_to_cols() {
  Pick p{};
  const char* name = "a-very-long-network-name-here";  // 29 chars
  std::memcpy(p.ssid, name, std::strlen(name));
  p.ssid_len = static_cast<uint8_t>(std::strlen(name));
  p.open = false;
  std::string r = row(p, 10);
  TEST_ASSERT_EQUAL_UINT(10, r.size());
  TEST_ASSERT_EQUAL_STRING("a-very-lon", r.c_str());
}

void test_row_open_marker_fits_within_cols() {
  Pick p{};
  const char* name = "longnetworkname";  // 15 chars
  std::memcpy(p.ssid, name, std::strlen(name));
  p.ssid_len = static_cast<uint8_t>(std::strlen(name));
  p.open = true;
  std::string r = row(p, 12);
  // Total must not exceed 12 and must end with the open marker.
  TEST_ASSERT_TRUE(r.size() <= 12);
  TEST_ASSERT_TRUE(r.find(" (open)") != std::string::npos);
}

// ---- runner --------------------------------------------------------------------------------------------

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_sort_by_rssi_descending);
  RUN_TEST(test_empty_input);
  RUN_TEST(test_dedupe_keeps_strongest_rssi);
  RUN_TEST(test_dedupe_weaker_second_sighting_ignored);
  RUN_TEST(test_hidden_networks_skipped);
  RUN_TEST(test_open_flag);
  RUN_TEST(test_overflow_keeps_strongest);
  RUN_TEST(test_overflow_weaker_newcomer_dropped);
  RUN_TEST(test_display_hidden);
  RUN_TEST(test_display_sanitises_nonprintable);
  RUN_TEST(test_display_truncates_to_buffer);
  RUN_TEST(test_display_full_32_byte_ssid);
  RUN_TEST(test_row_marks_open);
  RUN_TEST(test_row_secured_has_no_marker);
  RUN_TEST(test_row_truncates_to_cols);
  RUN_TEST(test_row_open_marker_fits_within_cols);
  return UNITY_END();
}
