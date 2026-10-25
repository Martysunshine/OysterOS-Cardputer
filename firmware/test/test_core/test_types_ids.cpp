// Wire names (types.h) and identifier validation/formatting (ids.h).
#include <unity.h>

#include <cstring>

#include "oyster_core/ids.h"
#include "oyster_core/types.h"
#include "test_common.h"

using namespace oyster;

static void test_event_status_wire_names() {
  TEST_ASSERT_EQUAL_STRING("success", to_string(EventStatus::Success));
  TEST_ASSERT_EQUAL_STRING("denied", to_string(EventStatus::Denied));
  TEST_ASSERT_EQUAL_STRING("timeout", to_string(EventStatus::Timeout));
  TEST_ASSERT_EQUAL_STRING("unsupported", to_string(EventStatus::Unsupported));
  TEST_ASSERT_EQUAL_STRING("error", to_string(EventStatus::Error));
  TEST_ASSERT_EQUAL_STRING("cancelled", to_string(EventStatus::Cancelled));
  TEST_ASSERT_EQUAL_STRING("partial", to_string(EventStatus::Partial));
  TEST_ASSERT_EQUAL_STRING("unknown_outcome", to_string(EventStatus::UnknownOutcome));
  TEST_ASSERT_EQUAL_STRING("inconclusive", to_string(EventStatus::Inconclusive));
}

static void test_operation_wire_names() {
  TEST_ASSERT_EQUAL_STRING("wifi_survey", to_string(Operation::WifiSurvey));
  TEST_ASSERT_EQUAL_STRING("ble_discover", to_string(Operation::BleDiscover));
  TEST_ASSERT_EQUAL_STRING("ble_inspect_gatt", to_string(Operation::BleInspectGatt));
  TEST_ASSERT_EQUAL_STRING("ble_read_profile_value", to_string(Operation::BleReadProfileValue));
  TEST_ASSERT_EQUAL_STRING("ble_observe_notifications", to_string(Operation::BleObserveNotifications));
  TEST_ASSERT_EQUAL_STRING("ble_check_access_policy", to_string(Operation::BleCheckAccessPolicy));
  TEST_ASSERT_EQUAL_STRING("ble_write_lab_value", to_string(Operation::BleWriteLabValue));
  TEST_ASSERT_EQUAL_STRING("ble_collect_diagnostics", to_string(Operation::BleCollectDiagnostics));
  TEST_ASSERT_EQUAL_STRING("boot", to_string(Operation::Boot));
  TEST_ASSERT_EQUAL_STRING("telemetry", to_string(Operation::Telemetry));
  TEST_ASSERT_EQUAL_STRING("session_open", to_string(Operation::SessionOpen));
  TEST_ASSERT_EQUAL_STRING("session_close", to_string(Operation::SessionClose));
  TEST_ASSERT_EQUAL_STRING("local_confirmation", to_string(Operation::LocalConfirmation));
  TEST_ASSERT_EQUAL_STRING("recovery", to_string(Operation::Recovery));
  TEST_ASSERT_EQUAL_STRING("unknown", to_string(Operation::Unknown));
}

static void test_other_enum_wire_names() {
  TEST_ASSERT_EQUAL_STRING("live", to_string(SourceMode::Live));
  TEST_ASSERT_EQUAL_STRING("offline", to_string(SourceMode::Offline));
  TEST_ASSERT_EQUAL_STRING("simulated", to_string(SourceMode::Simulated));
  TEST_ASSERT_EQUAL_STRING("wifi", to_string(Transport::Wifi));
  TEST_ASSERT_EQUAL_STRING("ble", to_string(Transport::Ble));
  TEST_ASSERT_EQUAL_STRING("backend", to_string(Transport::Backend));
  TEST_ASSERT_EQUAL_STRING("local", to_string(Transport::Local));
  TEST_ASSERT_EQUAL_STRING("none", to_string(Transport::None));
  TEST_ASSERT_EQUAL_STRING("ntp", to_string(ClockQuality::Ntp));
  TEST_ASSERT_EQUAL_STRING("serial", to_string(ClockQuality::Serial));
  TEST_ASSERT_EQUAL_STRING("backend_hint", to_string(ClockQuality::BackendHint));
  TEST_ASSERT_EQUAL_STRING("unknown", to_string(ClockQuality::Unknown));
  TEST_ASSERT_EQUAL_STRING("succeeded", to_string(JobOutcome::Succeeded));
  TEST_ASSERT_EQUAL_STRING("failed", to_string(JobOutcome::Failed));
  TEST_ASSERT_EQUAL_STRING("cancelled", to_string(JobOutcome::Cancelled));
  TEST_ASSERT_EQUAL_STRING("expired", to_string(JobOutcome::Expired));
  TEST_ASSERT_EQUAL_STRING("unknown_outcome", to_string(JobOutcome::UnknownOutcome));
  TEST_ASSERT_EQUAL_STRING("unsupported", to_string(JobOutcome::Unsupported));
  TEST_ASSERT_EQUAL_STRING("OPEN", to_string(WifiAuth::Open));
  TEST_ASSERT_EQUAL_STRING("WEP", to_string(WifiAuth::Wep));
  TEST_ASSERT_EQUAL_STRING("WPA_PSK", to_string(WifiAuth::WpaPsk));
  TEST_ASSERT_EQUAL_STRING("WPA2_PSK", to_string(WifiAuth::Wpa2Psk));
  TEST_ASSERT_EQUAL_STRING("WPA_WPA2_PSK", to_string(WifiAuth::WpaWpa2Psk));
  TEST_ASSERT_EQUAL_STRING("WPA2_ENTERPRISE", to_string(WifiAuth::Wpa2Enterprise));
  TEST_ASSERT_EQUAL_STRING("WPA3_PSK", to_string(WifiAuth::Wpa3Psk));
  TEST_ASSERT_EQUAL_STRING("WPA2_WPA3_PSK", to_string(WifiAuth::Wpa2Wpa3Psk));
  TEST_ASSERT_EQUAL_STRING("WAPI_PSK", to_string(WifiAuth::WapiPsk));
  TEST_ASSERT_EQUAL_STRING("UNKNOWN", to_string(WifiAuth::Unknown));
  TEST_ASSERT_EQUAL_STRING("public", to_string(BleAddrType::Public));
  TEST_ASSERT_EQUAL_STRING("random", to_string(BleAddrType::Random));
  TEST_ASSERT_EQUAL_STRING("unknown", to_string(BleAddrType::Unknown));
}

static void test_parse_operation_round_trip_job_ops_only() {
  for (uint8_t i = 0; i < kJobOperationCount; ++i) {
    const Operation op = static_cast<Operation>(i);
    Operation parsed = Operation::Unknown;
    TEST_ASSERT_TRUE(parse_operation(to_string(op), parsed));
    TEST_ASSERT_EQUAL_UINT8(i, static_cast<uint8_t>(parsed));
    TEST_ASSERT_TRUE(is_job_operation(op));
  }
  Operation out = Operation::WifiSurvey;
  TEST_ASSERT_FALSE(parse_operation("boot", out));
  TEST_ASSERT_FALSE(parse_operation("recovery", out));
  TEST_ASSERT_FALSE(parse_operation("unknown", out));
  TEST_ASSERT_FALSE(parse_operation("WIFI_SURVEY", out));
  TEST_ASSERT_FALSE(parse_operation("wifi_survey ", out));
  TEST_ASSERT_FALSE(parse_operation("", out));
  TEST_ASSERT_FALSE(parse_operation(nullptr, out));
  TEST_ASSERT_TRUE(out == Operation::WifiSurvey);  // untouched on failure
  TEST_ASSERT_FALSE(is_job_operation(Operation::Boot));
  TEST_ASSERT_FALSE(is_job_operation(Operation::Recovery));
  TEST_ASSERT_FALSE(is_job_operation(Operation::Unknown));
}

static void test_tribool_defaults_unknown() {
  TriBool t;
  TEST_ASSERT_FALSE(t.known());
  TEST_ASSERT_TRUE(TriBool::of(true).is_true());
  TEST_ASSERT_TRUE(TriBool::of(false).is_false());
  TEST_ASSERT_TRUE(TriBool::unknown() == t);
  SecurityState s;
  TEST_ASSERT_FALSE(s.encrypted.known());
  TEST_ASSERT_FALSE(s.authenticated_pairing.known());
  TEST_ASSERT_FALSE(s.application_authorized.known());
}

static void test_prefixed_id_validators() {
  TEST_ASSERT_TRUE(is_valid_session_id("sess-example-01"));
  TEST_ASSERT_TRUE(is_valid_session_id("sess-a"));
  TEST_ASSERT_FALSE(is_valid_session_id("sess-"));
  TEST_ASSERT_FALSE(is_valid_session_id("SESS-abc"));
  TEST_ASSERT_FALSE(is_valid_session_id("sess-a/b"));
  TEST_ASSERT_FALSE(is_valid_session_id("sess-a.b"));
  TEST_ASSERT_FALSE(is_valid_session_id("sess-a b"));
  TEST_ASSERT_FALSE(is_valid_session_id(""));
  TEST_ASSERT_FALSE(is_valid_session_id(nullptr));
  // 40 characters after the prefix is the maximum.
  char id[64] = "sess-";
  for (int i = 0; i < 40; ++i) std::strcat(id, "x");
  TEST_ASSERT_TRUE(is_valid_session_id(id));
  std::strcat(id, "x");
  TEST_ASSERT_FALSE(is_valid_session_id(id));

  TEST_ASSERT_TRUE(is_valid_job_id("job-000009"));
  TEST_ASSERT_FALSE(is_valid_job_id("job-../x"));
  TEST_ASSERT_FALSE(is_valid_job_id("sess-000009"));
  TEST_ASSERT_TRUE(is_valid_target_id("target-lab-01"));
  TEST_ASSERT_FALSE(is_valid_target_id("lab-01"));
  TEST_ASSERT_FALSE(is_valid_target_id("target-lab:01"));
  TEST_ASSERT_TRUE(is_prefixed_id("boot-1234", "boot-"));
  TEST_ASSERT_FALSE(is_prefixed_id("boot-1234", nullptr));
  TEST_ASSERT_FALSE(is_prefixed_id("boot-1234", ""));
}

static void test_unprefixed_id_validators() {
  TEST_ASSERT_TRUE(is_valid_device_id("sim-cardputer-01"));
  TEST_ASSERT_TRUE(is_valid_device_id("A_b-9"));
  TEST_ASSERT_FALSE(is_valid_device_id(""));
  TEST_ASSERT_FALSE(is_valid_device_id("dev 01"));
  TEST_ASSERT_FALSE(is_valid_device_id("dev\n01"));
  TEST_ASSERT_TRUE(is_valid_nonce("n-6f1c2a9e44b0"));
  TEST_ASSERT_FALSE(is_valid_nonce("n/6f"));
  TEST_ASSERT_FALSE(is_valid_nonce(nullptr));
  TEST_ASSERT_TRUE(is_safe_id_chars("abc", 3));
  TEST_ASSERT_FALSE(is_safe_id_chars("abcd", 3));
  TEST_ASSERT_FALSE(is_safe_id_chars("abc", 0));
  // Non-ASCII bytes are rejected.
  TEST_ASSERT_FALSE(is_valid_device_id("dev\xc3\xa9"));
}

static void test_object_id_rejects_traversal() {
  TEST_ASSERT_TRUE(is_valid_object_id("diag-synth-0001"));
  TEST_ASSERT_TRUE(is_valid_object_id("abcdefghijklmnop"));    // 16 characters
  TEST_ASSERT_FALSE(is_valid_object_id("abcdefghijklmnopq"));  // 17 characters
  TEST_ASSERT_FALSE(is_valid_object_id("../x"));
  TEST_ASSERT_FALSE(is_valid_object_id(".."));
  TEST_ASSERT_FALSE(is_valid_object_id("a/b"));
  TEST_ASSERT_FALSE(is_valid_object_id("a\\b"));
  TEST_ASSERT_FALSE(is_valid_object_id("/abs"));
  TEST_ASSERT_FALSE(is_valid_object_id("C:x"));
  TEST_ASSERT_FALSE(is_valid_object_id("a.bin"));
  TEST_ASSERT_FALSE(is_valid_object_id(""));
  TEST_ASSERT_FALSE(is_valid_object_id(nullptr));
}

static void test_format_event_id() {
  char out[24];
  TEST_ASSERT_TRUE(format_event_id(3, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("evt-000003", out);
  TEST_ASSERT_TRUE(format_event_id(0, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("evt-000000", out);
  TEST_ASSERT_TRUE(format_event_id(1234567, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("evt-1234567", out);
  TEST_ASSERT_TRUE(format_event_id(4294967295u, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("evt-4294967295", out);
  TEST_ASSERT_TRUE(format_event_id(17, out, 11));  // exactly fits "evt-000017" + NUL
  TEST_ASSERT_EQUAL_STRING("evt-000017", out);
  TEST_ASSERT_FALSE(format_event_id(17, out, 10));
  TEST_ASSERT_EQUAL_STRING("", out);
  TEST_ASSERT_FALSE(format_event_id(1, nullptr, 10));
}

static void test_format_boot_id() {
  char out[32];
  TEST_ASSERT_TRUE(format_boot_id(0xDEADBEEFu, 0x00000001u, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("boot-deadbeef00000001", out);
  TEST_ASSERT_TRUE(is_prefixed_id(out, "boot-"));
  TEST_ASSERT_TRUE(format_boot_id(0, 0, out, 22));
  TEST_ASSERT_EQUAL_STRING("boot-0000000000000000", out);
  TEST_ASSERT_FALSE(format_boot_id(1, 2, out, 21));
  TEST_ASSERT_EQUAL_STRING("", out);
}

static void test_copy_bounded() {
  char dst[6];
  TEST_ASSERT_TRUE(copy_bounded(dst, sizeof(dst), "abcde"));
  TEST_ASSERT_EQUAL_STRING("abcde", dst);
  TEST_ASSERT_FALSE(copy_bounded(dst, sizeof(dst), "abcdef"));
  TEST_ASSERT_EQUAL_STRING("abcde", dst);  // truncated prefix, still terminated
  TEST_ASSERT_TRUE(copy_bounded(dst, sizeof(dst), nullptr));
  TEST_ASSERT_EQUAL_STRING("", dst);
  TEST_ASSERT_FALSE(copy_bounded(nullptr, 4, "x"));
  TEST_ASSERT_FALSE(copy_bounded(dst, 0, "x"));
  char one[1];
  TEST_ASSERT_FALSE(copy_bounded(one, 1, "x"));
  TEST_ASSERT_EQUAL_STRING("", one);
}

void run_types_ids_tests() {
  RUN_TEST(test_event_status_wire_names);
  RUN_TEST(test_operation_wire_names);
  RUN_TEST(test_other_enum_wire_names);
  RUN_TEST(test_parse_operation_round_trip_job_ops_only);
  RUN_TEST(test_tribool_defaults_unknown);
  RUN_TEST(test_prefixed_id_validators);
  RUN_TEST(test_unprefixed_id_validators);
  RUN_TEST(test_object_id_rejects_traversal);
  RUN_TEST(test_format_event_id);
  RUN_TEST(test_format_boot_id);
  RUN_TEST(test_copy_bounded);
}
