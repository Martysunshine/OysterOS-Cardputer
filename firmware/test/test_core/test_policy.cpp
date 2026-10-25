// LocalPolicy (local_policy.h): every verdict, check order, confirmation binding, duplicate ledger.
#include <unity.h>

#include <cstdio>
#include <cstring>

#include "oyster_core/local_policy.h"
#include "test_common.h"

using namespace oyster;

static const char* kDevice = "dev-01";
static const char* kSession = "sess-test-01";

static EnrolledTarget g_targets[2];
static ExecutedJobLedger g_ledger;

static uint16_t all_target_ops_mask() {
  return static_cast<uint16_t>(op_bit(Operation::BleInspectGatt) | op_bit(Operation::BleReadProfileValue) |
                               op_bit(Operation::BleObserveNotifications) |
                               op_bit(Operation::BleCheckAccessPolicy) | op_bit(Operation::BleWriteLabValue) |
                               op_bit(Operation::BleCollectDiagnostics));
}

static void reset_targets() {
  std::memset(g_targets, 0, sizeof(g_targets));
  std::strcpy(g_targets[0].target_id, "target-lab-01");
  std::strcpy(g_targets[0].profile_id, "oyster-lab-v1");
  g_targets[0].scope_version = 1;
  g_targets[0].allowed_ops_mask = all_target_ops_mask();
  g_targets[0].address_type = BleAddrType::Unknown;
  std::strcpy(g_targets[1].target_id, "target-readonly");
  std::strcpy(g_targets[1].profile_id, "oyster-lab-v1");
  g_targets[1].scope_version = 3;
  g_targets[1].allowed_ops_mask =
      static_cast<uint16_t>(op_bit(Operation::BleInspectGatt) | op_bit(Operation::BleReadProfileValue));
  g_targets[1].address_type = BleAddrType::Unknown;
}

static JobSpec base_job(Operation op = Operation::BleReadProfileValue) {
  JobSpec j;
  std::memset(&j, 0, sizeof(j));
  std::strcpy(j.job_id, "job-000001");
  std::strcpy(j.session_id, kSession);
  std::strcpy(j.device_id, kDevice);
  std::strcpy(j.target_id, "target-lab-01");
  j.op = op;
  std::strcpy(j.op_wire, to_string(op));
  j.scope_version = 1;
  j.deadline_ms = 60000;
  j.received_ms = 1000;
  j.server_requires_confirmation = operation_requires_local_confirmation(op);
  std::strcpy(j.nonce, "n-6f1c2a9e44b0");
  std::strcpy(j.args_json, "{}");
  j.args_len = 2;
  return j;
}

static PolicyInput base_input() {
  PolicyInput in;
  in.device_id = kDevice;
  in.active_session_id = kSession;
  in.now_ms = 2000;
  in.backend_authenticated = true;
  in.targets = g_targets;
  in.target_count = 2;
  return in;
}

static PolicyVerdict verdict(const JobSpec& j, const PolicyInput& in) { return evaluate_job(j, in, g_ledger).verdict; }

#define ASSERT_VERDICT(expected, actual) \
  TEST_ASSERT_EQUAL_STRING(to_string(PolicyVerdict::expected), to_string(actual))

static void policy_setup() {
  reset_targets();
  g_ledger.clear();
}

static void test_operation_classification() {
  struct Row {
    Operation op;
    bool confirm, target, changes;
  };
  const Row rows[] = {
      {Operation::WifiSurvey, false, false, false},
      {Operation::BleDiscover, false, false, false},
      {Operation::BleInspectGatt, false, true, false},
      {Operation::BleReadProfileValue, false, true, false},
      {Operation::BleObserveNotifications, false, true, true},
      {Operation::BleCheckAccessPolicy, false, true, true},  // assertions may write/transfer: never auto-retried
      {Operation::BleWriteLabValue, true, true, true},
      {Operation::BleCollectDiagnostics, true, true, true},
  };
  for (const Row& r : rows) {
    TEST_ASSERT_EQUAL_MESSAGE(r.confirm, operation_requires_local_confirmation(r.op), to_string(r.op));
    TEST_ASSERT_EQUAL_MESSAGE(r.target, operation_requires_target(r.op), to_string(r.op));
    TEST_ASSERT_EQUAL_MESSAGE(r.changes, operation_changes_target_state(r.op), to_string(r.op));
  }
  TEST_ASSERT_FALSE(operation_requires_local_confirmation(Operation::Unknown));
  TEST_ASSERT_FALSE(operation_requires_target(Operation::Boot));
}

static void test_accept_target_operation() {
  policy_setup();
  const JobSpec j = base_job();
  const PolicyDecision d = evaluate_job(j, base_input(), g_ledger);
  ASSERT_VERDICT(Accept, d.verdict);
  TEST_ASSERT_FALSE(d.requires_local_confirmation);
  TEST_ASSERT_TRUE(d.target == &g_targets[0]);
  TEST_ASSERT_NOT_NULL(d.reason);
}

static void test_accept_targetless_operation() {
  policy_setup();
  JobSpec j = base_job(Operation::WifiSurvey);
  j.target_id[0] = '\0';
  j.device_id[0] = '\0';  // device_id is optional
  j.nonce[0] = '\0';      // nonce is optional for non-confirmed operations
  j.limits.max_duration_ms = 30000;
  const PolicyDecision d = evaluate_job(j, base_input(), g_ledger);
  ASSERT_VERDICT(Accept, d.verdict);
  TEST_ASSERT_NULL(d.target);
  TEST_ASSERT_FALSE(d.requires_local_confirmation);
}

static void test_reject_malformed() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  std::strcpy(j.job_id, "job-../x");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  j.job_id[0] = '\0';
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  std::strcpy(j.session_id, "session-1");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  std::strcpy(j.target_id, "lab-01");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  std::strcpy(j.device_id, "dev 01");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  std::strcpy(j.nonce, "n/1");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  j = base_job();
  j.scope_version = 0;
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  // A confirmed operation without a nonce could never be confirmed.
  j = base_job(Operation::BleWriteLabValue);
  j.nonce[0] = '\0';
  const PolicyDecision d = evaluate_job(j, in, g_ledger);
  ASSERT_VERDICT(RejectMalformed, d.verdict);
  TEST_ASSERT_TRUE(d.requires_local_confirmation);
  // An unterminated id buffer ("job-" + 44 safe chars, no NUL) is rejected by the bounded scan.
  j = base_job();
  std::memset(j.job_id, 'a', sizeof(j.job_id));
  std::memcpy(j.job_id, "job-", 4);
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
}

static void test_reject_unknown_operation() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  j.op = Operation::Unknown;
  ASSERT_VERDICT(RejectUnknownOperation, verdict(j, in));
  j.op = Operation::Boot;  // device-local kinds are not job operations
  ASSERT_VERDICT(RejectUnknownOperation, verdict(j, in));
  j.op = Operation::Recovery;
  ASSERT_VERDICT(RejectUnknownOperation, verdict(j, in));
}

static void test_reject_wrong_device() {
  policy_setup();
  PolicyInput in = base_input();
  JobSpec j = base_job();
  std::strcpy(j.device_id, "dev-02");
  ASSERT_VERDICT(RejectWrongDevice, verdict(j, in));
  std::strcpy(j.device_id, "dev-0");  // prefix of the real id
  ASSERT_VERDICT(RejectWrongDevice, verdict(j, in));
  std::strcpy(j.device_id, kDevice);
  in.device_id = nullptr;
  ASSERT_VERDICT(RejectWrongDevice, verdict(j, in));
}

static void test_reject_wrong_session() {
  policy_setup();
  PolicyInput in = base_input();
  JobSpec j = base_job();
  std::strcpy(j.session_id, "sess-other");
  ASSERT_VERDICT(RejectWrongSession, verdict(j, in));
  j = base_job();
  in.active_session_id = "";  // no session open
  ASSERT_VERDICT(RejectWrongSession, verdict(j, in));
  in.active_session_id = nullptr;
  ASSERT_VERDICT(RejectWrongSession, verdict(j, in));
}

static void test_reject_not_authenticated() {
  policy_setup();
  PolicyInput in = base_input();
  in.backend_authenticated = false;
  ASSERT_VERDICT(RejectNotAuthenticated, verdict(base_job(), in));
}

static void test_reject_duplicate_via_ledger() {
  policy_setup();
  const PolicyInput in = base_input();
  const JobSpec j = base_job();
  ASSERT_VERDICT(Accept, verdict(j, in));
  g_ledger.record(j.job_id);
  ASSERT_VERDICT(RejectDuplicate, verdict(j, in));
  JobSpec other = base_job();
  std::strcpy(other.job_id, "job-000002");
  ASSERT_VERDICT(Accept, verdict(other, in));
}

static void test_ledger_eviction_at_capacity() {
  ExecutedJobLedger ledger;
  char id[cfg::kIdBufLen];
  for (size_t i = 0; i < ExecutedJobLedger::kCapacity; ++i) {
    std::snprintf(id, sizeof(id), "job-%03u", static_cast<unsigned>(i));
    ledger.record(id);
  }
  TEST_ASSERT_EQUAL_UINT32(ExecutedJobLedger::kCapacity, ledger.size());
  TEST_ASSERT_TRUE(ledger.contains("job-000"));
  TEST_ASSERT_TRUE(ledger.contains("job-031"));
  // Re-recording a known id changes nothing.
  ledger.record("job-000");
  TEST_ASSERT_EQUAL_UINT32(ExecutedJobLedger::kCapacity, ledger.size());
  TEST_ASSERT_TRUE(ledger.contains("job-000"));
  // One more evicts the oldest only.
  ledger.record("job-new");
  TEST_ASSERT_EQUAL_UINT32(ExecutedJobLedger::kCapacity, ledger.size());
  TEST_ASSERT_FALSE(ledger.contains("job-000"));
  TEST_ASSERT_TRUE(ledger.contains("job-001"));
  TEST_ASSERT_TRUE(ledger.contains("job-new"));
  ledger.record("job-new2");
  TEST_ASSERT_FALSE(ledger.contains("job-001"));
  TEST_ASSERT_TRUE(ledger.contains("job-002"));
  TEST_ASSERT_FALSE(ledger.contains(""));
  TEST_ASSERT_FALSE(ledger.contains(nullptr));
  ledger.record("");
  ledger.record(nullptr);
  TEST_ASSERT_EQUAL_UINT32(ExecutedJobLedger::kCapacity, ledger.size());
  ledger.clear();
  TEST_ASSERT_EQUAL_UINT32(0, ledger.size());
  TEST_ASSERT_FALSE(ledger.contains("job-new"));
}

static void test_reject_target_required_and_unknown() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  j.target_id[0] = '\0';
  ASSERT_VERDICT(RejectTargetRequired, verdict(j, in));
  j = base_job(Operation::BleWriteLabValue);
  j.target_id[0] = '\0';
  ASSERT_VERDICT(RejectTargetRequired, verdict(j, in));
  j = base_job();
  std::strcpy(j.target_id, "target-other");
  ASSERT_VERDICT(RejectUnknownTarget, verdict(j, in));
  // A target-less operation that names a target is still bound to an enrolled one.
  j = base_job(Operation::BleDiscover);
  std::strcpy(j.target_id, "target-other");
  ASSERT_VERDICT(RejectUnknownTarget, verdict(j, in));
  // No enrolled targets at all.
  PolicyInput none = in;
  none.targets = nullptr;
  none.target_count = 0;
  ASSERT_VERDICT(RejectUnknownTarget, verdict(base_job(), none));
}

static void test_reject_operation_not_allowed() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job(Operation::BleWriteLabValue);
  std::strcpy(j.target_id, "target-readonly");
  j.scope_version = 3;
  const PolicyDecision d = evaluate_job(j, in, g_ledger);
  ASSERT_VERDICT(RejectOperationNotAllowed, d.verdict);
  TEST_ASSERT_TRUE(d.target == &g_targets[1]);
  j = base_job(Operation::BleReadProfileValue);
  std::strcpy(j.target_id, "target-readonly");
  j.scope_version = 3;
  ASSERT_VERDICT(Accept, verdict(j, in));
  // ble_discover naming a target whose allow-list lacks it.
  j = base_job(Operation::BleDiscover);
  ASSERT_VERDICT(RejectOperationNotAllowed, verdict(j, in));
}

static void test_reject_stale_scope() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  j.scope_version = 2;
  ASSERT_VERDICT(RejectStaleScope, verdict(j, in));
  g_targets[0].scope_version = 2;  // owner raised the scope: the job now matches
  ASSERT_VERDICT(Accept, verdict(j, in));
}

static void test_reject_expired() {
  policy_setup();
  PolicyInput in = base_input();
  JobSpec j = base_job();
  in.now_ms = j.received_ms + j.deadline_ms;  // boundary: still valid
  ASSERT_VERDICT(Accept, verdict(j, in));
  in.now_ms = j.received_ms + j.deadline_ms + 1;
  ASSERT_VERDICT(RejectExpired, verdict(j, in));
  in = base_input();
  j.deadline_ms = 0;
  ASSERT_VERDICT(RejectExpired, verdict(j, in));
}

static void test_reject_args_too_large() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  j.args_len = cfg::kMaxJobArgsBytes;
  ASSERT_VERDICT(Accept, verdict(j, in));
  j.args_len = cfg::kMaxJobArgsBytes + 1;
  ASSERT_VERDICT(RejectArgsTooLarge, verdict(j, in));
}

static void test_reject_limits_exceeded() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job();
  j.limits.max_results = 257;
  ASSERT_VERDICT(RejectLimitsExceeded, verdict(j, in));
  j = base_job();
  j.limits.max_bytes = cfg::kMaxDiagObjectBytes + 1;
  ASSERT_VERDICT(RejectLimitsExceeded, verdict(j, in));
  j = base_job();
  j.deadline_ms = cfg::kMaxJobDurationMs + 1;
  ASSERT_VERDICT(RejectLimitsExceeded, verdict(j, in));
  j = base_job(Operation::WifiSurvey);
  j.target_id[0] = '\0';
  j.limits.max_duration_ms = 30001;
  ASSERT_VERDICT(RejectLimitsExceeded, verdict(j, in));
  j.limits.max_duration_ms = 30000;
  ASSERT_VERDICT(Accept, verdict(j, in));
}

static void test_limits_within_bounds() {
  JobLimits l{};
  TEST_ASSERT_TRUE(limits_within_bounds(Operation::WifiSurvey, l));  // 0 = not specified
  l.max_results = 256;
  l.max_bytes = cfg::kMaxDiagObjectBytes;
  l.max_duration_ms = 30000;
  TEST_ASSERT_TRUE(limits_within_bounds(Operation::BleDiscover, l));
  l.max_duration_ms = 30001;
  TEST_ASSERT_FALSE(limits_within_bounds(Operation::BleDiscover, l));
  TEST_ASSERT_FALSE(limits_within_bounds(Operation::WifiSurvey, l));
  TEST_ASSERT_TRUE(limits_within_bounds(Operation::BleCollectDiagnostics, l));  // not a scan
  l.max_duration_ms = cfg::kMaxJobDurationMs + 1;
  TEST_ASSERT_FALSE(limits_within_bounds(Operation::BleCollectDiagnostics, l));
  l = JobLimits{};
  l.max_results = 257;
  TEST_ASSERT_FALSE(limits_within_bounds(Operation::BleDiscover, l));
  l = JobLimits{};
  l.max_bytes = cfg::kMaxDiagObjectBytes + 1;
  TEST_ASSERT_FALSE(limits_within_bounds(Operation::BleCollectDiagnostics, l));
}

static void test_check_order_is_deterministic() {
  policy_setup();
  PolicyInput in = base_input();
  // malformed beats unknown operation
  JobSpec j = base_job();
  j.op = Operation::Unknown;
  std::strcpy(j.session_id, "bad");
  ASSERT_VERDICT(RejectMalformed, verdict(j, in));
  // unknown operation beats wrong device
  j = base_job();
  j.op = Operation::Unknown;
  std::strcpy(j.device_id, "dev-02");
  ASSERT_VERDICT(RejectUnknownOperation, verdict(j, in));
  // wrong device beats wrong session
  j = base_job();
  std::strcpy(j.device_id, "dev-02");
  std::strcpy(j.session_id, "sess-other");
  ASSERT_VERDICT(RejectWrongDevice, verdict(j, in));
  // wrong session beats not authenticated
  j = base_job();
  std::strcpy(j.session_id, "sess-other");
  in.backend_authenticated = false;
  ASSERT_VERDICT(RejectWrongSession, verdict(j, in));
  // not authenticated beats duplicate
  j = base_job();
  g_ledger.record(j.job_id);
  ASSERT_VERDICT(RejectNotAuthenticated, verdict(j, in));
  in.backend_authenticated = true;
  // duplicate beats unknown target
  std::strcpy(j.target_id, "target-other");
  ASSERT_VERDICT(RejectDuplicate, verdict(j, in));
  g_ledger.clear();
  // unknown target beats expiry
  in.now_ms = 10000000;
  ASSERT_VERDICT(RejectUnknownTarget, verdict(j, in));
  // operation-not-allowed beats stale scope
  j = base_job(Operation::BleWriteLabValue);
  std::strcpy(j.target_id, "target-readonly");
  j.scope_version = 99;
  ASSERT_VERDICT(RejectOperationNotAllowed, verdict(j, in));
  // stale scope beats expired
  j = base_job();
  j.scope_version = 99;
  ASSERT_VERDICT(RejectStaleScope, verdict(j, in));
  // expired beats args too large
  j = base_job();
  j.args_len = 9999;
  ASSERT_VERDICT(RejectExpired, verdict(j, in));
  in = base_input();
  // args too large beats limits
  j.limits.max_results = 9999;
  ASSERT_VERDICT(RejectArgsTooLarge, verdict(j, in));
}

static void test_write_job_requires_confirmation_even_if_server_says_no() {
  policy_setup();
  const PolicyInput in = base_input();
  JobSpec j = base_job(Operation::BleWriteLabValue);
  j.server_requires_confirmation = false;  // backend/model flag tries to lower the requirement
  PolicyDecision d = evaluate_job(j, in, g_ledger);
  ASSERT_VERDICT(Accept, d.verdict);
  TEST_ASSERT_TRUE(d.requires_local_confirmation);

  j = base_job(Operation::BleCollectDiagnostics);
  j.server_requires_confirmation = false;
  d = evaluate_job(j, in, g_ledger);
  ASSERT_VERDICT(Accept, d.verdict);
  TEST_ASSERT_TRUE(d.requires_local_confirmation);

  // The flag is derived from the operation only, also on rejected jobs and in both directions.
  j = base_job(Operation::BleWriteLabValue);
  j.server_requires_confirmation = false;
  std::strcpy(j.session_id, "sess-other");
  d = evaluate_job(j, in, g_ledger);
  ASSERT_VERDICT(RejectWrongSession, d.verdict);
  TEST_ASSERT_TRUE(d.requires_local_confirmation);
  j = base_job(Operation::BleReadProfileValue);
  j.server_requires_confirmation = true;
  d = evaluate_job(j, in, g_ledger);
  TEST_ASSERT_FALSE(d.requires_local_confirmation);
}

static LocalConfirmation make_confirmation(const JobSpec& j) {
  LocalConfirmation c;
  std::memset(&c, 0, sizeof(c));
  std::strcpy(c.job_id, j.job_id);
  std::strcpy(c.nonce, j.nonce);
  c.approved = true;
  c.decided_ms = j.received_ms + 5000;
  return c;
}

static void test_confirmation_valid_and_invalid() {
  const JobSpec j = base_job(Operation::BleWriteLabValue);
  const uint32_t timeout = cfg::kLocalConfirmTimeoutMs;
  LocalConfirmation c = make_confirmation(j);
  TEST_ASSERT_TRUE(confirmation_valid(j, c, timeout));

  c = make_confirmation(j);
  std::strcpy(c.nonce, "n-6f1c2a9e44b1");  // wrong nonce
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));
  c = make_confirmation(j);
  std::strcpy(c.nonce, "n-6f1c2a9e44b");  // prefix of the nonce
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));
  c = make_confirmation(j);
  std::strcpy(c.job_id, "job-000002");  // wrong job
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));
  c = make_confirmation(j);
  c.approved = false;  // declined
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));
  c = make_confirmation(j);
  c.decided_ms = j.received_ms + timeout;  // boundary: on time
  TEST_ASSERT_TRUE(confirmation_valid(j, c, timeout));
  c.decided_ms = j.received_ms + timeout + 1;  // late
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));
  c = make_confirmation(j);
  c.decided_ms = j.received_ms - 1;  // decided before the job was received
  TEST_ASSERT_FALSE(confirmation_valid(j, c, timeout));

  // An empty nonce never matches, even if both sides are empty.
  JobSpec no_nonce = j;
  no_nonce.nonce[0] = '\0';
  c = make_confirmation(no_nonce);
  TEST_ASSERT_FALSE(confirmation_valid(no_nonce, c, timeout));
  // The server flag is irrelevant to confirmation validity.
  JobSpec flag = j;
  flag.server_requires_confirmation = false;
  c = make_confirmation(flag);
  c.approved = false;
  TEST_ASSERT_FALSE(confirmation_valid(flag, c, timeout));
}

static void test_policy_verdict_names() {
  const PolicyVerdict all[] = {
      PolicyVerdict::Accept, PolicyVerdict::RejectMalformed, PolicyVerdict::RejectUnknownOperation,
      PolicyVerdict::RejectWrongDevice, PolicyVerdict::RejectWrongSession, PolicyVerdict::RejectTargetRequired,
      PolicyVerdict::RejectUnknownTarget, PolicyVerdict::RejectOperationNotAllowed, PolicyVerdict::RejectStaleScope,
      PolicyVerdict::RejectExpired, PolicyVerdict::RejectArgsTooLarge, PolicyVerdict::RejectLimitsExceeded,
      PolicyVerdict::RejectDuplicate, PolicyVerdict::RejectNotAuthenticated,
  };
  const size_t n = sizeof(all) / sizeof(all[0]);
  for (size_t i = 0; i < n; ++i) {
    TEST_ASSERT_NOT_NULL(to_string(all[i]));
    for (size_t k = i + 1; k < n; ++k) TEST_ASSERT_TRUE(std::strcmp(to_string(all[i]), to_string(all[k])) != 0);
  }
  TEST_ASSERT_EQUAL_STRING("accept", to_string(PolicyVerdict::Accept));
  TEST_ASSERT_EQUAL_STRING("reject_stale_scope", to_string(PolicyVerdict::RejectStaleScope));
}

void run_policy_tests() {
  RUN_TEST(test_operation_classification);
  RUN_TEST(test_accept_target_operation);
  RUN_TEST(test_accept_targetless_operation);
  RUN_TEST(test_reject_malformed);
  RUN_TEST(test_reject_unknown_operation);
  RUN_TEST(test_reject_wrong_device);
  RUN_TEST(test_reject_wrong_session);
  RUN_TEST(test_reject_not_authenticated);
  RUN_TEST(test_reject_duplicate_via_ledger);
  RUN_TEST(test_ledger_eviction_at_capacity);
  RUN_TEST(test_reject_target_required_and_unknown);
  RUN_TEST(test_reject_operation_not_allowed);
  RUN_TEST(test_reject_stale_scope);
  RUN_TEST(test_reject_expired);
  RUN_TEST(test_reject_args_too_large);
  RUN_TEST(test_reject_limits_exceeded);
  RUN_TEST(test_limits_within_bounds);
  RUN_TEST(test_check_order_is_deterministic);
  RUN_TEST(test_write_job_requires_confirmation_even_if_server_says_no);
  RUN_TEST(test_confirmation_valid_and_invalid);
  RUN_TEST(test_policy_verdict_names);
}
