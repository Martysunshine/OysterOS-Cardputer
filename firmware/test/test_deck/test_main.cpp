// Host unit tests of the Control Deck wire format (oyster_core/deck_json.h).
// Run: pio test -e native -f test_deck
#include <unity.h>

#include "test_common.h"

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  run_deck_parse_tests();
  run_deck_text_tests();
  run_deck_hostile_tests();
  return UNITY_END();
}
