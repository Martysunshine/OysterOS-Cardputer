// Lab device_info identity string and fail-closed target resolution (oyster_core/lab_identity.h).
#include <unity.h>

#include <cstdio>
#include <cstring>

#include "oyster_core/lab_identity.h"
#include "oyster_core/lab_profile_gen.h"
#include "test_common.h"

using namespace oyster;

static const uint8_t kSvcUuid[16] = {0x86, 0x2f, 0x20, 0xd9, 0xfa, 0x73, 0x5f, 0x89,
                                     0xb1, 0x30, 0x86, 0xee, 0x93, 0xe7, 0x82, 0x61};
static const uint8_t kOtherUuid[16] = {0x18, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00,
                                       0x80, 0x00, 0x00, 0x80, 0x5f, 0x9b, 0x34, 0xfb};

static bool matches(const char* s, size_t n) { return lab_device_info_matches(reinterpret_cast<const uint8_t*>(s), n); }

// ---- device_info ------------------------------------------------------------------------------------

static void test_device_info_expected_is_profile_id_and_version() {
  char buf[48];
  const size_t n = lab_device_info_expected(buf, sizeof(buf));
  TEST_ASSERT_EQUAL_UINT32(15, n);
  TEST_ASSERT_EQUAL_STRING("oyster-lab-v1;1", buf);  // schemas/lab-profile.json: "oyster-lab-v1;1"
  char built[48];
  std::snprintf(built, sizeof(built), "%s;%lu", lab::kProfileId, static_cast<unsigned long>(lab::kProfileVersion));
  TEST_ASSERT_EQUAL_STRING(built, buf);
  // Too small a buffer is reported as 0, never a truncated string.
  char tiny[8];
  TEST_ASSERT_EQUAL_UINT32(0, lab_device_info_expected(tiny, sizeof(tiny)));
  TEST_ASSERT_EQUAL_UINT32(0, lab_device_info_expected(nullptr, 10));
}

static void test_device_info_exact_match() {
  TEST_ASSERT_TRUE(matches("oyster-lab-v1;1", 15));
}

static void test_device_info_tolerates_one_trailing_nul_only() {
  TEST_ASSERT_TRUE(matches("oyster-lab-v1;1\0", 16));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;1\0\0", 17));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;1\0x", 17));
  // The terminator position must really be NUL.
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;1X", 16));
}

static void test_device_info_old_bare_profile_id_is_rejected() {
  TEST_ASSERT_FALSE(matches("oyster-lab-v1", 13));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;", 14));
}

static void test_device_info_extra_or_changed_bytes_rejected() {
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;1x", 16));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;10", 16));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;2", 15));
  TEST_ASSERT_FALSE(matches("oyster-lab-v2;1", 15));
  TEST_ASSERT_FALSE(matches("Oyster-lab-v1;1", 15));
  TEST_ASSERT_FALSE(matches(" oyster-lab-v1;1", 16));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1;1\n", 16));
  TEST_ASSERT_FALSE(matches("oyster-lab-v1\0;1", 15));  // embedded NUL
}

static void test_device_info_empty_null_and_long_rejected() {
  TEST_ASSERT_FALSE(lab_device_info_matches(nullptr, 0));
  TEST_ASSERT_FALSE(lab_device_info_matches(nullptr, 15));
  TEST_ASSERT_FALSE(matches("", 0));
  TEST_ASSERT_FALSE(matches("\0", 1));
  uint8_t big[200];
  std::memset(big, 'a', sizeof(big));
  TEST_ASSERT_FALSE(lab_device_info_matches(big, sizeof(big)));
  std::memcpy(big, "oyster-lab-v1;1", 15);
  TEST_ASSERT_FALSE(lab_device_info_matches(big, sizeof(big)));  // right prefix, too long
  TEST_ASSERT_FALSE(lab_device_info_matches(big, 32));           // max_length of the characteristic
}

// ---- remote read allow-list --------------------------------------------------------------------------

static void test_remote_read_allows_only_public_side_effect_free_characteristics() {
  TEST_ASSERT_TRUE(lab_remote_read_allowed("public_sensor"));
  TEST_ASSERT_TRUE(lab_remote_read_allowed("diag_status"));
  TEST_ASSERT_TRUE(lab_remote_read_allowed("virtual_led"));
  TEST_ASSERT_TRUE(lab_remote_read_allowed("device_info"));
}

static void test_remote_read_refuses_protected_and_stateful_characteristics() {
  TEST_ASSERT_FALSE(lab_remote_read_allowed("restricted_enc"));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("restricted_mitm"));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("diag_manifest"));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("chunk_data"));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("chunk_ctrl"));  // no read policy at all
  // Public by policy, but a read replaces the fixture's single outstanding challenge.
  const lab::CharDef* app_auth = lab::find_char("app_auth");
  TEST_ASSERT_NOT_NULL(app_auth);
  TEST_ASSERT_TRUE(app_auth->read == lab::Policy::Public);
  TEST_ASSERT_FALSE(lab_remote_read_allowed("app_auth"));
}

static void test_remote_read_refuses_unknown_and_malformed_ids() {
  TEST_ASSERT_FALSE(lab_remote_read_allowed(nullptr));
  TEST_ASSERT_FALSE(lab_remote_read_allowed(""));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("nope"));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("public_sensor "));
  TEST_ASSERT_FALSE(lab_remote_read_allowed("PUBLIC_SENSOR"));
}

// ---- target resolution --------------------------------------------------------------------------------

static EnrolledTarget make_target(bool addr, bool uuid, const char* name) {
  EnrolledTarget t;
  std::memset(&t, 0, sizeof(t));
  std::strcpy(t.target_id, "target-lab-01");
  std::strcpy(t.profile_id, "oyster-lab-v1");
  t.address_type = BleAddrType::Unknown;
  if (addr) {
    const uint8_t a[6] = {0xde, 0xad, 0xbe, 0xef, 0x00, 0x01};
    std::memcpy(t.address, a, 6);
    t.has_address = true;
    t.address_type = BleAddrType::Random;
  }
  if (uuid) {
    std::memcpy(t.service_uuid128, kSvcUuid, 16);
    t.has_service_uuid = true;
  }
  if (name != nullptr) std::strncpy(t.advertised_name, name, sizeof(t.advertised_name) - 1);
  return t;
}

static BleAdvObservation make_obs(uint8_t last, const char* name, const uint8_t* uuid) {
  BleAdvObservation o{};
  o.addr[0] = 0x11;
  o.addr[5] = last;
  o.addr_type = BleAddrType::Public;
  o.connectable = TriBool::of(true);
  if (name != nullptr) {
    o.name_len = static_cast<uint8_t>(std::strlen(name));
    std::memcpy(o.name, name, o.name_len);
  }
  if (uuid != nullptr) {
    o.svc_uuid_count = 1;
    std::memcpy(o.svc_uuid128[0], uuid, 16);
  }
  return o;
}

static void test_resolve_enrolled_address_ignores_discovery_rows() {
  const EnrolledTarget t = make_target(true, true, "Oyster-Lab");
  TargetResolver r(t);
  // Two look-alikes are present, but the enrolled address is the only candidate.
  r.add(make_obs(1, "Oyster-Lab", kSvcUuid));
  r.add(make_obs(2, "Oyster-Lab", kSvcUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL(static_cast<int>(TargetMatch::EnrolledAddress), static_cast<int>(x.rule));
  TEST_ASSERT_EQUAL_UINT32(1, x.candidates);
  TEST_ASSERT_EQUAL_UINT8(0xde, x.address[0]);
  TEST_ASSERT_EQUAL_UINT8(0x01, x.address[5]);
  TEST_ASSERT_EQUAL(static_cast<int>(BleAddrType::Random), static_cast<int>(x.address_type));
}

static void test_resolve_enrolled_address_needs_no_discovery() {
  const EnrolledTarget t = make_target(true, false, nullptr);
  TargetResolver r(t);
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(r.finish().result));
}

static void test_resolve_single_service_uuid_match() {
  const EnrolledTarget t = make_target(false, true, nullptr);
  TargetResolver r(t);
  r.add(make_obs(1, "other", kOtherUuid));
  r.add(make_obs(7, nullptr, kSvcUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL(static_cast<int>(TargetMatch::ServiceUuid), static_cast<int>(x.rule));
  TEST_ASSERT_EQUAL_UINT8(7, x.address[5]);
  TEST_ASSERT_EQUAL(static_cast<int>(BleAddrType::Public), static_cast<int>(x.address_type));
  TEST_ASSERT_EQUAL_STRING("service_uuid", to_string(x.rule));
}

static void test_resolve_single_name_match() {
  const EnrolledTarget t = make_target(false, false, "Oyster-Lab");
  TargetResolver r(t);
  r.add(make_obs(1, "Oyster-Lab-2", nullptr));  // different name
  r.add(make_obs(2, "oyster-lab", nullptr));    // case differs
  r.add(make_obs(3, "Oyster-Lab", nullptr));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL(static_cast<int>(TargetMatch::NameHint), static_cast<int>(x.rule));
  TEST_ASSERT_EQUAL_UINT8(3, x.address[5]);
  TEST_ASSERT_EQUAL_STRING("name_hint", to_string(x.rule));
}

static void test_resolve_two_same_uuid_is_ambiguous() {
  const EnrolledTarget t = make_target(false, true, nullptr);
  TargetResolver r(t);
  r.add(make_obs(1, nullptr, kSvcUuid));
  r.add(make_obs(2, nullptr, kSvcUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Ambiguous), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL_UINT32(2, x.candidates);
}

static void test_resolve_two_same_name_is_ambiguous() {
  const EnrolledTarget t = make_target(false, false, "Oyster-Lab");
  TargetResolver r(t);
  r.add(make_obs(1, "Oyster-Lab", nullptr));
  r.add(make_obs(2, "Oyster-Lab", nullptr));
  r.add(make_obs(3, "Oyster-Lab", nullptr));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Ambiguous), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL_UINT32(3, x.candidates);
}

static void test_resolve_uuid_and_name_on_different_devices_is_ambiguous() {
  // The union keeps a look-alike with the right name from being silently ignored (or preferred).
  const EnrolledTarget t = make_target(false, true, "Oyster-Lab");
  TargetResolver r(t);
  r.add(make_obs(1, nullptr, kSvcUuid));
  r.add(make_obs(2, "Oyster-Lab", nullptr));
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Ambiguous), static_cast<int>(r.finish().result));
}

static void test_resolve_one_device_matching_both_rules_is_one_candidate() {
  const EnrolledTarget t = make_target(false, true, "Oyster-Lab");
  TargetResolver r(t);
  r.add(make_obs(1, "Oyster-Lab", kSvcUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL_UINT32(1, x.candidates);
  TEST_ASSERT_EQUAL(static_cast<int>(TargetMatch::ServiceUuid), static_cast<int>(x.rule));
}

static void test_resolve_none_and_empty_name_do_not_match() {
  EnrolledTarget t = make_target(false, true, nullptr);  // empty advertised name
  TargetResolver r(t);
  r.add(make_obs(1, nullptr, nullptr));                  // no name, no uuid
  r.add(make_obs(2, "x", kOtherUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::NotObserved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL_UINT32(0, x.candidates);
  // A target with nothing to match on is never resolved.
  const EnrolledTarget bare = make_target(false, false, nullptr);
  TargetResolver rb(bare);
  rb.add(make_obs(1, nullptr, nullptr));
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::NotObserved), static_cast<int>(rb.finish().result));
}

static void test_resolve_skips_non_connectable_devices() {
  const EnrolledTarget t = make_target(false, true, nullptr);
  TargetResolver r(t);
  BleAdvObservation beacon = make_obs(1, nullptr, kSvcUuid);
  beacon.connectable = TriBool::of(false);
  r.add(beacon);
  r.add(make_obs(2, nullptr, kSvcUuid));
  const TargetResolution x = r.finish();
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Resolved), static_cast<int>(x.result));
  TEST_ASSERT_EQUAL_UINT8(2, x.address[5]);
  // Unknown connectability is still a candidate (never assume it cannot be dialled).
  TargetResolver r2(t);
  BleAdvObservation unk = make_obs(3, nullptr, kSvcUuid);
  unk.connectable = TriBool::unknown();
  r2.add(unk);
  r2.add(make_obs(4, nullptr, kSvcUuid));
  TEST_ASSERT_EQUAL(static_cast<int>(ResolveResult::Ambiguous), static_cast<int>(r2.finish().result));
}

void run_lab_identity_tests() {
  RUN_TEST(test_device_info_expected_is_profile_id_and_version);
  RUN_TEST(test_device_info_exact_match);
  RUN_TEST(test_device_info_tolerates_one_trailing_nul_only);
  RUN_TEST(test_device_info_old_bare_profile_id_is_rejected);
  RUN_TEST(test_device_info_extra_or_changed_bytes_rejected);
  RUN_TEST(test_device_info_empty_null_and_long_rejected);
  RUN_TEST(test_remote_read_allows_only_public_side_effect_free_characteristics);
  RUN_TEST(test_remote_read_refuses_protected_and_stateful_characteristics);
  RUN_TEST(test_remote_read_refuses_unknown_and_malformed_ids);
  RUN_TEST(test_resolve_enrolled_address_ignores_discovery_rows);
  RUN_TEST(test_resolve_enrolled_address_needs_no_discovery);
  RUN_TEST(test_resolve_single_service_uuid_match);
  RUN_TEST(test_resolve_single_name_match);
  RUN_TEST(test_resolve_two_same_uuid_is_ambiguous);
  RUN_TEST(test_resolve_two_same_name_is_ambiguous);
  RUN_TEST(test_resolve_uuid_and_name_on_different_devices_is_ambiguous);
  RUN_TEST(test_resolve_one_device_matching_both_rules_is_one_candidate);
  RUN_TEST(test_resolve_none_and_empty_name_do_not_match);
  RUN_TEST(test_resolve_skips_non_connectable_devices);
}
