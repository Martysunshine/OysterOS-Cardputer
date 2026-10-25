// Placeholder until this module is built (docs/UI_SCREEN_MAP.md). It shows an honest text and does nothing
// else: no network, no state. Replaced by the real module.
#include <cstdio>

#include "modules.h"

namespace oyster::modules {
namespace {

bool available(const char** why) {
  (void)why;
  return true;
}

void open() {}

void view(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = 0;
  std::snprintf(v.title, sizeof(v.title), "Smart Home: not built yet");
  std::snprintf(v.body, sizeof(v.body),
                "Not built yet.\nIR remote and Home Assistant. Nothing on this screen runs or connects.");
  std::snprintf(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
}

void select(uint16_t row) { (void)row; }

bool back() { return false; }

void tile_status(char* out, size_t n) { std::snprintf(out, n, "soon"); }

}  // namespace

const Ops kSmartHomeOps = {"Smart Home", available, open, view, select, back, nullptr, nullptr, nullptr, nullptr, tile_status};

}  // namespace oyster::modules
