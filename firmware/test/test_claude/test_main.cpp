// Host unit tests of the Claude Code link on the device: wire format (oyster_core/claude_json.h) and the
// Control Deck module paths that use it (src/modules/deck.cpp driven through its Ops table).
// Run: pio test -e native -f test_claude
#include <unity.h>

#include "test_common.h"

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  run_claude_parse_tests();
  run_claude_text_tests();
  run_claude_hostile_tests();
  run_claude_module_tests();
  return UNITY_END();
}
