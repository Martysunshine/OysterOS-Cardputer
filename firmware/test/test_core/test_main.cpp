// Host unit tests of lib/oyster_core (PlatformIO env:native, Unity).
#include <unity.h>

#include "test_common.h"

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  run_types_ids_tests();
  run_sha256_tests();
  run_json_tests();
  run_jsonl_tests();
  run_policy_tests();
  run_transfer_tests();
  run_lab_identity_tests();
  run_event_paging_tests();
  run_delivery_tests();
  return UNITY_END();
}
