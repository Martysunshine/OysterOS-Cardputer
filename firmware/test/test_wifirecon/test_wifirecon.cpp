// Host tests for the Wi-Fi recon module's pure logic (oyster_core/wifi_recon.h): the accumulating,
// BSSID-deduplicated AP table (merge, max/last RSSI, times_seen, hidden-name fill, capacity + weakest
// eviction), RSSI-descending sort, and the display/CSV formatters (auth label, BSSID string, SSID
// sanitising, row clipping, relative time, CSV field escaping). Run: pio test -e native -f test_wifirecon
#include <unity.h>

#include <cstring>
#include <string>

#include "oyster_core/wifi_recon.h"

using namespace oyster;
using oyster::wifi_recon::ApEntry;
using oyster::wifi_recon::ApTable;
using oyster::wifi_recon::Merge;

void setUp() {}
void tearDown() {}

namespace {

// Build an observation with an ASCII SSID and a single-byte-varying BSSID.
ApObservation ap(const char* ssid, int8_t rssi, uint8_t bssid_last, uint8_t channel = 6,
                 WifiAuth auth = WifiAuth::Wpa2Psk) {
  ApObservation o{};
  const size_t len = ssid == nullptr ? 0 : std::strlen(ssid);
  o.ssid_len = static_cast<uint8_t>(len > 32 ? 32 : len);
  if (o.ssid_len > 0) std::memcpy(o.ssid, ssid, o.ssid_len);
  o.rssi_dbm = rssi;
  o.auth = auth;
  o.channel = channel;
  const uint8_t b[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, bssid_last};
  std::memcpy(o.bssid, b, 6);
  return o;
}

std::string disp(const uint8_t* s, uint8_t len) {
  char buf[64];
  wifi_recon::display_ssid(s, len, buf, sizeof(buf));
  return std::string(buf);
}

std::string row(const ApEntry& e, size_t cols) {
  char buf[64];
  wifi_recon::format_row(e, buf, sizeof(buf), cols);
  return std::string(buf);
}

std::string csv(const ApObservation& o, uint32_t ts) {
  char buf[128];
  wifi_recon::format_csv_line(o, ts, buf, sizeof(buf));
  return std::string(buf);
}

std::string escape(const char* ssid) {
  char buf[96];
  wifi_recon::csv_escape_ssid(reinterpret_cast<const uint8_t*>(ssid), static_cast<uint8_t>(std::strlen(ssid)),
                              buf, sizeof(buf));
  return std::string(buf);
}

std::string rel(uint32_t delta_ms) {
  char buf[24];
  wifi_recon::format_relative(delta_ms, buf, sizeof(buf));
  return std::string(buf);
}

}  // namespace

// ---- merge / dedupe -----------------------------------------------------------------------------------

void test_merge_adds_distinct_bssids() {
  ApTable t;
  TEST_ASSERT_EQUAL(static_cast<int>(Merge::Added), static_cast<int>(t.merge(ap("A", -50, 1), 100)));
  TEST_ASSERT_EQUAL(static_cast<int>(Merge::Added), static_cast<int>(t.merge(ap("B", -60, 2), 100)));
  TEST_ASSERT_EQUAL_size_t(2, t.count());
}

void test_merge_dedupes_by_bssid() {
  ApTable t;
  t.merge(ap("Net", -70, 7), 1000);
  const Merge m = t.merge(ap("Net", -55, 7), 4000);  // same BSSID, stronger, later
  TEST_ASSERT_EQUAL(static_cast<int>(Merge::Updated), static_cast<int>(m));
  TEST_ASSERT_EQUAL_size_t(1, t.count());
  const ApEntry& e = t.at(0);
  TEST_ASSERT_EQUAL_INT8(-55, e.max_rssi);
  TEST_ASSERT_EQUAL_INT8(-55, e.last_rssi);
  TEST_ASSERT_EQUAL_UINT16(2, e.times_seen);
  TEST_ASSERT_EQUAL_UINT32(1000, e.first_seen_ms);
  TEST_ASSERT_EQUAL_UINT32(4000, e.last_seen_ms);
}

void test_merge_keeps_max_but_tracks_last() {
  ApTable t;
  t.merge(ap("Net", -40, 3), 10);
  t.merge(ap("Net", -80, 3), 20);  // weaker sighting: max stays, last drops
  const ApEntry& e = t.at(0);
  TEST_ASSERT_EQUAL_INT8(-40, e.max_rssi);
  TEST_ASSERT_EQUAL_INT8(-80, e.last_rssi);
  TEST_ASSERT_EQUAL_UINT16(2, e.times_seen);
}

void test_hidden_then_named_fills_ssid() {
  ApTable t;
  t.merge(ap(nullptr, -50, 9), 0);  // hidden (ssid_len 0)
  TEST_ASSERT_EQUAL_UINT8(0, t.at(0).ssid_len);
  t.merge(ap("Revealed", -50, 9), 100);  // same BSSID now broadcasts a name
  const ApEntry& e = t.at(0);
  TEST_ASSERT_EQUAL_UINT8(8, e.ssid_len);
  TEST_ASSERT_EQUAL_STRING("Revealed", disp(e.ssid, e.ssid_len).c_str());
  TEST_ASSERT_EQUAL_size_t(1, t.count());
}

void test_clear_resets() {
  ApTable t;
  t.merge(ap("A", -50, 1), 0);
  t.clear();
  TEST_ASSERT_EQUAL_size_t(0, t.count());
  TEST_ASSERT_EQUAL_UINT32(0, t.dropped());
}

void test_find_by_bssid() {
  ApTable t;
  t.merge(ap("A", -50, 1), 0);
  t.merge(ap("B", -60, 2), 0);
  const uint8_t want[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 2};
  const uint8_t miss[6] = {0, 0, 0, 0, 0, 0};
  TEST_ASSERT_EQUAL_INT(1, t.find(want));
  TEST_ASSERT_EQUAL_INT(-1, t.find(miss));
}

// ---- capacity + eviction ------------------------------------------------------------------------------

void test_capacity_full_evicts_weakest_for_stronger() {
  ApTable t;
  for (size_t i = 0; i < ApTable::capacity(); ++i) {
    // One deliberately weak entry (last byte 0 at -90); the rest at -50.
    t.merge(ap("x", i == 0 ? -90 : -50, static_cast<uint8_t>(i)), static_cast<uint32_t>(i));
  }
  TEST_ASSERT_EQUAL_size_t(ApTable::capacity(), t.count());
  const uint8_t weak[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0};
  TEST_ASSERT_TRUE(t.find(weak) >= 0);

  const Merge m = t.merge(ap("newcomer", -40, 200), 9999);  // stronger than the -90 entry
  TEST_ASSERT_EQUAL(static_cast<int>(Merge::Added), static_cast<int>(m));
  TEST_ASSERT_EQUAL_size_t(ApTable::capacity(), t.count());
  TEST_ASSERT_EQUAL_INT(-1, t.find(weak));  // the weakest was evicted
  const uint8_t nc[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 200};
  TEST_ASSERT_TRUE(t.find(nc) >= 0);
  TEST_ASSERT_EQUAL_UINT32(1, t.dropped());
}

void test_capacity_full_drops_weaker_newcomer() {
  ApTable t;
  for (size_t i = 0; i < ApTable::capacity(); ++i) {
    t.merge(ap("x", -50, static_cast<uint8_t>(i)), static_cast<uint32_t>(i));
  }
  const Merge m = t.merge(ap("faint", -95, 201), 9999);  // weaker than every stored entry
  TEST_ASSERT_EQUAL(static_cast<int>(Merge::Dropped), static_cast<int>(m));
  TEST_ASSERT_EQUAL_size_t(ApTable::capacity(), t.count());
  const uint8_t nc[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 201};
  TEST_ASSERT_EQUAL_INT(-1, t.find(nc));
  TEST_ASSERT_EQUAL_UINT32(1, t.dropped());
}

// ---- sort ---------------------------------------------------------------------------------------------

void test_sort_by_rssi_descending() {
  ApTable t;
  t.merge(ap("weak", -80, 1), 0);
  t.merge(ap("strong", -40, 2), 0);
  t.merge(ap("mid", -60, 3), 0);
  t.sort_by_rssi();
  TEST_ASSERT_EQUAL_INT8(-40, t.at(0).max_rssi);
  TEST_ASSERT_EQUAL_INT8(-60, t.at(1).max_rssi);
  TEST_ASSERT_EQUAL_INT8(-80, t.at(2).max_rssi);
}

// ---- display / row formatting -------------------------------------------------------------------------

void test_display_hidden_and_nonprintable() {
  TEST_ASSERT_EQUAL_STRING("<hidden>", disp(nullptr, 0).c_str());
  const uint8_t raw[4] = {'H', 0x01, 'i', 0x7F};  // 0x01 and 0x7F are non-printable
  TEST_ASSERT_EQUAL_STRING("H?i?", disp(raw, 4).c_str());
}

void test_auth_short_labels() {
  TEST_ASSERT_EQUAL_STRING("OPEN", wifi_recon::auth_short(WifiAuth::Open));
  TEST_ASSERT_EQUAL_STRING("WPA2", wifi_recon::auth_short(WifiAuth::Wpa2Psk));
  TEST_ASSERT_EQUAL_STRING("WPA3", wifi_recon::auth_short(WifiAuth::Wpa2Wpa3Psk));
  TEST_ASSERT_EQUAL_STRING("?", wifi_recon::auth_short(WifiAuth::Unknown));
}

void test_bssid_to_str() {
  const uint8_t b[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF};
  char out[18];
  wifi_recon::bssid_to_str(b, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("aa:bb:cc:dd:ee:ff", out);
}

void test_row_format_and_clip() {
  ApTable t;
  t.merge(ap("MyNetworkName", -54, 1, 6), 0);
  const std::string full = row(t.at(0), 40);
  TEST_ASSERT_EQUAL_STRING("-54 ch6 WPA2 MyNetworkName", full.c_str());
  // Clip to 16 visible characters.
  const std::string clipped = row(t.at(0), 16);
  TEST_ASSERT_EQUAL_size_t(16, clipped.size());
  TEST_ASSERT_EQUAL_STRING("-54 ch6 WPA2 MyN", clipped.c_str());
}

void test_row_hidden() {
  ApTable t;
  t.merge(ap(nullptr, -70, 1, 11, WifiAuth::Open), 0);
  TEST_ASSERT_EQUAL_STRING("-70 ch11 OPEN <hidden>", row(t.at(0), 40).c_str());
}

// ---- relative time ------------------------------------------------------------------------------------

void test_relative_time() {
  TEST_ASSERT_EQUAL_STRING("now", rel(0).c_str());
  TEST_ASSERT_EQUAL_STRING("now", rel(999).c_str());
  TEST_ASSERT_EQUAL_STRING("12s ago", rel(12000).c_str());
  TEST_ASSERT_EQUAL_STRING("3m ago", rel(180000).c_str());
  TEST_ASSERT_EQUAL_STRING("2h ago", rel(2u * 3600u * 1000u).c_str());
  TEST_ASSERT_EQUAL_STRING("1d ago", rel(90000u * 1000u).c_str());
}

// ---- CSV escaping -------------------------------------------------------------------------------------

void test_csv_escape_plain() { TEST_ASSERT_EQUAL_STRING("Simple", escape("Simple").c_str()); }

void test_csv_escape_comma() {
  TEST_ASSERT_EQUAL_STRING("\"a,b\"", escape("a,b").c_str());
}

void test_csv_escape_quote() {
  // A single internal quote is doubled and the whole field is wrapped in quotes: say"hi -> "say""hi"
  TEST_ASSERT_EQUAL_STRING("\"say\"\"hi\"", escape("say\"hi").c_str());
}

void test_csv_escape_newline_becomes_question() {
  const uint8_t raw[3] = {'a', '\n', 'b'};  // newline is non-printable -> '?', so no quoting needed
  char out[32];
  wifi_recon::csv_escape_ssid(raw, 3, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("a?b", out);
}

void test_csv_escape_hidden_empty() {
  char out[8];
  wifi_recon::csv_escape_ssid(nullptr, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("", out);
}

void test_csv_line_fields() {
  const std::string line = csv(ap("Net", -54, 0xFF, 6, WifiAuth::Wpa2Psk), 1000);
  TEST_ASSERT_EQUAL_STRING("1000,aa:bb:cc:dd:ee:ff,6,-54,WPA2,0,Net", line.c_str());
}

void test_csv_line_hidden() {
  const std::string line = csv(ap(nullptr, -70, 0x01, 3, WifiAuth::Open), 42);
  TEST_ASSERT_EQUAL_STRING("42,aa:bb:cc:dd:ee:01,3,-70,OPEN,1,", line.c_str());
}

void test_csv_line_ssid_with_comma_is_quoted() {
  const std::string line = csv(ap("a,b", -40, 0x02, 1, WifiAuth::Wpa3Psk), 7);
  TEST_ASSERT_EQUAL_STRING("7,aa:bb:cc:dd:ee:02,1,-40,WPA3,0,\"a,b\"", line.c_str());
}

void test_csv_header() {
  TEST_ASSERT_EQUAL_STRING("ts,bssid,channel,rssi,auth,hidden,ssid", wifi_recon::csv_header());
}

// ---- runner -------------------------------------------------------------------------------------------

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_merge_adds_distinct_bssids);
  RUN_TEST(test_merge_dedupes_by_bssid);
  RUN_TEST(test_merge_keeps_max_but_tracks_last);
  RUN_TEST(test_hidden_then_named_fills_ssid);
  RUN_TEST(test_clear_resets);
  RUN_TEST(test_find_by_bssid);
  RUN_TEST(test_capacity_full_evicts_weakest_for_stronger);
  RUN_TEST(test_capacity_full_drops_weaker_newcomer);
  RUN_TEST(test_sort_by_rssi_descending);
  RUN_TEST(test_display_hidden_and_nonprintable);
  RUN_TEST(test_auth_short_labels);
  RUN_TEST(test_bssid_to_str);
  RUN_TEST(test_row_format_and_clip);
  RUN_TEST(test_row_hidden);
  RUN_TEST(test_relative_time);
  RUN_TEST(test_csv_escape_plain);
  RUN_TEST(test_csv_escape_comma);
  RUN_TEST(test_csv_escape_quote);
  RUN_TEST(test_csv_escape_newline_becomes_question);
  RUN_TEST(test_csv_escape_hidden_empty);
  RUN_TEST(test_csv_line_fields);
  RUN_TEST(test_csv_line_hidden);
  RUN_TEST(test_csv_line_ssid_with_comma_is_quoted);
  RUN_TEST(test_csv_header);
  return UNITY_END();
}
