// Adaptive evidence-page building (event_paging.h) and the observation encoders (obs_json.h): at the
// worst-case field widths no observation is lost and no event line exceeds cfg::kMaxEventBytes.
#include <unity.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "oyster_core/event_paging.h"
#include "oyster_core/ids.h"
#include "oyster_core/obs_json.h"
#include "test_common.h"

using namespace oyster;

namespace {

// Largest envelope EvidenceStore::append_body prefixes (same format string, maximal ids).
size_t max_envelope_bytes() {
  char dev[41];
  std::memset(dev, 'd', 40);
  dev[40] = '\0';
  char sess[46] = "sess-";
  std::memset(sess + 5, 's', 40);
  sess[45] = '\0';
  char boot[48] = "boot-";
  std::memset(boot + 5, 'b', 40);  // longer than the 16 hex digits the device really uses
  boot[45] = '\0';
  char eid[cfg::kIdBufLen];
  format_event_id(4294967295u, eid, sizeof(eid));
  char pre[512];
  const int n = std::snprintf(pre, sizeof(pre),
                              "{\"schema_version\":\"1.0\",\"event_id\":\"%s\",\"session_id\":\"%s\",\"device_id\":\"%s\","
                              "\"boot_id\":\"%s\",\"sequence\":%lu,",
                              eid, sess, dev, boot, 4294967295ul);
  return static_cast<size_t>(n);
}

// begin_event() header at the widest values it can take, up to and including `"details":{`.
void write_worst_header(JsonWriter& w) {
  char target[cfg::kIdBufLen];
  std::memset(target, 't', cfg::kIdBufLen - 1);
  target[cfg::kIdBufLen - 1] = '\0';
  w.begin_object();
  w.str("source_mode", "simulated");
  w.str("transport", "backend");
  w.str("target_id", target);
  w.str("operation", to_string(Operation::BleObserveNotifications));
  w.str("timestamp_utc", "2026-10-24T10:00:00.123Z");
  w.str("clock_quality", "backend_hint");
  w.unum("uptime_ms", UINT64_MAX);
  w.str("status", "unknown_outcome");
  w.begin_object("details");
}

ApObservation worst_ap(uint16_t idx) {
  ApObservation ap;
  std::memset(&ap, 0, sizeof(ap));
  std::memset(ap.ssid, '"', cfg::kMaxSsidBytes);  // printable, but every byte is escaped in JSON
  ap.ssid_len = cfg::kMaxSsidBytes;
  ap.bssid[0] = 0xaa;
  ap.bssid[4] = static_cast<uint8_t>(idx >> 8);
  ap.bssid[5] = static_cast<uint8_t>(idx & 0xff);
  ap.channel = 14;
  ap.rssi_dbm = -100;
  ap.auth = WifiAuth::Wpa2Enterprise;
  return ap;
}

BleAdvObservation worst_adv(uint16_t idx) {
  BleAdvObservation o{};
  o.obs_id = idx;
  std::memset(o.addr, 0xee, 6);
  o.addr_type = BleAddrType::Random;
  o.rssi_dbm = -127;
  o.connectable = TriBool::of(false);
  std::memset(o.name, '"', cfg::kMaxBleNameBytes);
  o.name_len = cfg::kMaxBleNameBytes;
  o.svc_uuid_count = cfg::kMaxSvcUuidsPerAdv;
  o.svc_uuids_truncated = true;
  for (uint8_t k = 0; k < cfg::kMaxSvcUuidsPerAdv; ++k) std::memset(o.svc_uuid128[k], 0xf0 + k, 16);
  std::memset(o.mfg_data, 0xff, cfg::kMaxMfgDataBytes);
  o.mfg_len = cfg::kMaxMfgDataBytes;
  o.mfg_truncated = true;
  o.malformed = true;
  o.first_seen_ms = 4294967295u;
  o.last_seen_ms = 4294967295u;
  o.seen_count = 65535;
  return o;
}

GattEntry worst_gatt(uint16_t idx) {
  GattEntry e;
  std::memset(&e, 0, sizeof(e));
  e.kind = GattKind::Characteristic;
  std::memset(e.uuid128, 0xab, 16);
  e.uuid_bits = 128;
  e.handle = static_cast<uint16_t>(65535 - idx);
  e.props = 0xff;
  e.parent = 127;
  return e;
}

size_t item_len_ap() {
  char buf[512];
  JsonWriter w(buf, sizeof(buf));
  write_ap_json(w, worst_ap(1));
  return w.ok() ? w.size() : 0;
}
size_t item_len_adv() {
  char buf[1024];
  JsonWriter w(buf, sizeof(buf));
  write_adv_json(w, worst_adv(1));
  return w.ok() ? w.size() : 0;
}

// Builds the pages of `count` elements the way operations.cpp does and checks every page.
// Returns the element indices (as recovered from the JSON, using `id_key`/`bssid`) in page order.
struct Built {
  size_t pages = 0;
  size_t dropped = 0;
  size_t max_line = 0;
  std::vector<int> ids;
  bool all_ok = true;
};

void collect_numbers(const char* json, const char* key, std::vector<int>& out) {
  const size_t kl = std::strlen(key);
  for (const char* p = std::strstr(json, key); p != nullptr; p = std::strstr(p + kl, key)) {
    out.push_back(static_cast<int>(std::strtol(p + kl, nullptr, 10)));
  }
}

void collect_bssid_ids(const char* json, std::vector<int>& out) {
  const char* key = "\"bssid\":\"aa:00:00:00:";
  const size_t kl = std::strlen(key);
  for (const char* p = std::strstr(json, key); p != nullptr; p = std::strstr(p + kl, key)) {
    const int hi = static_cast<int>(std::strtol(p + kl, nullptr, 16));
    const int lo = static_cast<int>(std::strtol(p + kl + 3, nullptr, 16));
    out.push_back(hi * 256 + lo);
  }
}

template <typename WriteItem>
Built build_all(size_t count, bool ap, WriteItem&& write_item) {
  Built b;
  static char scratch[kEventScratchBytes];
  const size_t env = max_envelope_bytes();
  size_t i = 0;
  size_t guard = 0;
  while (i < count && guard++ < 1000) {
    JsonWriter w(scratch, kEventScratchBytes);
    write_worst_header(w);
    w.unum("page_index", 65535);
    w.boolean("more_pages", false);
    w.unum("first_index", 65535);
    w.begin_array("observations");
    const size_t before = i;
    i = fill_array_page(w, scratch, kPageArrayBytes, i, count, write_item, b.dropped);
    w.end_array();
    w.end_object();  // details
    w.end_object();  // envelope
    if (!w.ok()) b.all_ok = false;
    const size_t line = env + (w.size() - 1) + 1;  // body without its '{', plus '\n'
    if (line > b.max_line) b.max_line = line;
    if (ap) {
      collect_bssid_ids(scratch, b.ids);
    } else {
      collect_numbers(scratch, "\"obs_id\":", b.ids);
    }
    ++b.pages;
    if (i == before) break;
  }
  return b;
}

}  // namespace

static void test_event_limits_are_consistent() {
  TEST_ASSERT_EQUAL_UINT32(1792, kEventScratchBytes);
  TEST_ASSERT_TRUE(max_envelope_bytes() <= kEventEnvelopeReserve);
  // Worst-case header stays inside its budget.
  char buf[kEventScratchBytes];
  JsonWriter w(buf, sizeof(buf));
  write_worst_header(w);
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_TRUE(w.size() <= kEventHeaderBudget);
  // Page array budget leaves room for the page fields and the closing braces.
  JsonWriter f(buf, sizeof(buf));
  f.begin_object();
  f.begin_object("details");
  const size_t base = f.size();
  f.unum("page_index", 65535);
  f.boolean("more_pages", false);
  f.unum("first_index", 65535);
  f.begin_array("observations");
  f.end_array();
  f.end_object();  // details
  f.end_object();  // envelope
  TEST_ASSERT_TRUE(f.ok());
  TEST_ASSERT_TRUE(f.size() - base <= kPageFieldsBudget);
  TEST_ASSERT_TRUE(kEventHeaderBudget + kPageFieldsBudget + kPageArrayBytes <= kEventScratchBytes);
}

static void test_worst_case_items_fit_an_empty_page() {
  const size_t ap = item_len_ap();
  const size_t adv = item_len_adv();
  TEST_ASSERT_TRUE(ap > 200);                  // sanity: the worst case really is wide
  TEST_ASSERT_TRUE(adv > 450);
  TEST_ASSERT_TRUE(ap < kPageArrayBytes);
  TEST_ASSERT_TRUE(adv < kPageArrayBytes);
  char buf[512];
  JsonWriter w(buf, sizeof(buf));
  write_gatt_entry_json(w, worst_gatt(0));
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_TRUE(w.size() < 160);
}

static void test_old_fixed_page_sizes_overflowed_the_event_limit() {
  // Regression test: 8 worst-case APs or 3 worst-case adverts per event exceeded
  // cfg::kMaxEventBytes once the envelope was added, and EvidenceStore dropped the event.
  const size_t env = max_envelope_bytes();
  char buf[4096];
  {
    JsonWriter w(buf, sizeof(buf));
    write_worst_header(w);
    w.begin_array("observations");
    for (int i = 0; i < 8; ++i) write_ap_json(w, worst_ap(static_cast<uint16_t>(i)));
    w.end_array();
    w.end_object();
    w.end_object();
    TEST_ASSERT_TRUE(env + w.size() > cfg::kMaxEventBytes);
  }
  {
    JsonWriter w(buf, sizeof(buf));
    write_worst_header(w);
    w.begin_array("observations");
    for (int i = 0; i < 3; ++i) write_adv_json(w, worst_adv(static_cast<uint16_t>(i)));
    w.end_array();
    w.end_object();
    w.end_object();
    TEST_ASSERT_TRUE(env + w.size() > cfg::kMaxEventBytes);
  }
}

static void test_all_worst_case_aps_are_stored_in_order() {
  const size_t n = cfg::kMaxApObservations;  // 32
  ApObservation aps[cfg::kMaxApObservations];
  for (size_t i = 0; i < n; ++i) aps[i] = worst_ap(static_cast<uint16_t>(i));
  Built b = build_all(n, true, [&](JsonWriter& w, size_t i) { write_ap_json(w, aps[i]); });
  TEST_ASSERT_TRUE(b.all_ok);
  TEST_ASSERT_EQUAL_UINT32(0, b.dropped);
  TEST_ASSERT_TRUE(b.max_line <= cfg::kMaxEventBytes);
  TEST_ASSERT_EQUAL_UINT32(n, b.ids.size());
  for (size_t i = 0; i < n; ++i) TEST_ASSERT_EQUAL_INT(static_cast<int>(i), b.ids[i]);
  TEST_ASSERT_TRUE(b.pages > 1);
}

static void test_all_worst_case_adverts_are_stored_in_order() {
  const size_t n = cfg::kMaxBleAdverts;  // 64
  static BleAdvObservation adv[cfg::kMaxBleAdverts];
  for (size_t i = 0; i < n; ++i) adv[i] = worst_adv(static_cast<uint16_t>(i + 1));
  Built b = build_all(n, false, [&](JsonWriter& w, size_t i) { write_adv_json(w, adv[i]); });
  TEST_ASSERT_TRUE(b.all_ok);
  TEST_ASSERT_EQUAL_UINT32(0, b.dropped);
  TEST_ASSERT_TRUE(b.max_line <= cfg::kMaxEventBytes);
  TEST_ASSERT_EQUAL_UINT32(n, b.ids.size());
  for (size_t i = 0; i < n; ++i) TEST_ASSERT_EQUAL_INT(static_cast<int>(i + 1), b.ids[i]);
}

static void test_short_items_pack_densely() {
  // Typical short observations use far fewer pages than the worst case.
  const size_t n = cfg::kMaxApObservations;
  ApObservation aps[cfg::kMaxApObservations];
  for (size_t i = 0; i < n; ++i) {
    std::memset(&aps[i], 0, sizeof(aps[i]));
    std::memcpy(aps[i].ssid, "Home", 4);
    aps[i].ssid_len = 4;
    aps[i].bssid[0] = 0xaa;
    aps[i].bssid[5] = static_cast<uint8_t>(i);
    aps[i].channel = 6;
    aps[i].rssi_dbm = -60;
    aps[i].auth = WifiAuth::Wpa2Psk;
  }
  Built dense = build_all(n, true, [&](JsonWriter& w, size_t i) { write_ap_json(w, aps[i]); });
  TEST_ASSERT_EQUAL_UINT32(n, dense.ids.size());
  Built worst = build_all(n, true, [&](JsonWriter& w, size_t i) { write_ap_json(w, worst_ap(static_cast<uint16_t>(i))); });
  TEST_ASSERT_TRUE(dense.pages < worst.pages);
  TEST_ASSERT_TRUE(dense.pages <= 5);
}

static void test_plan_matches_real_run() {
  const size_t n = 40;
  static char scratch[kEventScratchBytes];
  auto write_item = [&](JsonWriter& w, size_t i) { write_adv_json(w, worst_adv(static_cast<uint16_t>(i + 1))); };
  PagePlan plan = plan_pages(scratch, kPageArrayBytes, n, write_item);
  Built real = build_all(n, false, write_item);
  TEST_ASSERT_EQUAL_UINT32(real.pages, plan.pages);
  TEST_ASSERT_EQUAL_UINT32(0, plan.dropped);
  TEST_ASSERT_EQUAL_UINT32(0, real.dropped);
}

static void test_plan_of_nothing_is_zero_pages() {
  static char scratch[kEventScratchBytes];
  PagePlan plan = plan_pages(scratch, kPageArrayBytes, 0, [](JsonWriter&, size_t) {});
  TEST_ASSERT_EQUAL_UINT32(0, plan.pages);
  TEST_ASSERT_EQUAL_UINT32(0, plan.dropped);
}

static void test_unstorable_item_is_counted_not_silently_lost() {
  // Element 3 is wider than any page: it is skipped and counted, its neighbours are still stored.
  const size_t n = 6;
  auto write_item = [](JsonWriter& w, size_t i) {
    w.begin_object();
    w.unum("obs_id", i);
    if (i == 3) {
      char big[kPageArrayBytes + 64];
      std::memset(big, 'x', sizeof(big) - 1);
      big[sizeof(big) - 1] = '\0';
      w.str("pad", big);
    }
    w.end_object();
  };
  static char scratch[kEventScratchBytes];
  PagePlan plan = plan_pages(scratch, kPageArrayBytes, n, write_item);
  TEST_ASSERT_EQUAL_UINT32(1, plan.dropped);
  Built b = build_all(n, false, write_item);
  TEST_ASSERT_TRUE(b.all_ok);
  TEST_ASSERT_EQUAL_UINT32(1, b.dropped);
  const int expect[5] = {0, 1, 2, 4, 5};
  TEST_ASSERT_EQUAL_UINT32(5, b.ids.size());
  for (size_t i = 0; i < 5; ++i) TEST_ASSERT_EQUAL_INT(expect[i], b.ids[i]);
}

static void test_fill_restores_writer_after_rejected_item() {
  static char scratch[kEventScratchBytes];
  JsonWriter w(scratch, kEventScratchBytes);
  w.begin_array();
  size_t dropped = 0;
  // Every element is ~1 KB, so only one fits per page; the writer must stay valid JSON afterwards.
  auto write_item = [](JsonWriter& jw, size_t i) {
    char pad[900];
    std::memset(pad, 'p', sizeof(pad) - 1);
    pad[sizeof(pad) - 1] = '\0';
    jw.begin_object();
    jw.unum("i", i);
    jw.str("pad", pad);
    jw.end_object();
  };
  const size_t next = fill_array_page(w, scratch, kPageArrayBytes, 0, 5, write_item, dropped);
  TEST_ASSERT_EQUAL_UINT32(1, next);
  TEST_ASSERT_EQUAL_UINT32(0, dropped);
  TEST_ASSERT_TRUE(w.ok());
  w.end_array();
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_CHAR(']', scratch[w.size() - 1]);
  TEST_ASSERT_EQUAL_CHAR('}', scratch[w.size() - 2]);
  TEST_ASSERT_EQUAL_INT(0, scratch[w.size()]);  // NUL-terminated at the restored length
  TEST_ASSERT_NULL(std::strstr(scratch, "\"i\":1"));
}

void run_event_paging_tests() {
  RUN_TEST(test_event_limits_are_consistent);
  RUN_TEST(test_worst_case_items_fit_an_empty_page);
  RUN_TEST(test_old_fixed_page_sizes_overflowed_the_event_limit);
  RUN_TEST(test_all_worst_case_aps_are_stored_in_order);
  RUN_TEST(test_all_worst_case_adverts_are_stored_in_order);
  RUN_TEST(test_short_items_pack_densely);
  RUN_TEST(test_plan_matches_real_run);
  RUN_TEST(test_plan_of_nothing_is_zero_pages);
  RUN_TEST(test_unstorable_item_is_counted_not_silently_lost);
  RUN_TEST(test_fill_restores_writer_after_rejected_item);
}
