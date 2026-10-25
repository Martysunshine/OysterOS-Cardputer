// Pure keyboard-to-navigation mapping (host-testable). BoardSupport fills a KeySnapshot from
// M5Cardputer.Keyboard.keysState() each time the key set changes. The logical actions follow the
// OysterOS contract (docs/OysterOS_Project_Plan.md section 4): Up/Down/Left/Right, Select, Back, Home,
// Help, Cancel, plus Char/Erase for text entry.
//
// Proposed mapping - UNVERIFIED, MUST be confirmed on hardware and recorded in docs/HARDWARE_VERIFICATION.md
// (Settings > Key tester shows the physical keys and the resolved action):
// M5Cardputer 1.1.1 KeysState has no arrow/esc/backspace fields and its `del` is the Backspace key:
// BoardSupport derives up/down/left/right from Fn + ; . , / and esc from Fn+` (there is no plain Esc key).
//   Cancel (any screen, any mode): Fn+` (reported as fn+esc) or Ctrl+C
//   Home   (any screen, any mode): Fn+Del (Fn+Backspace), proposed in the plan once validated
//   Navigation mode: Up/Down/Left/Right = Fn + ; . , / or bare ; . , /
//                    Select = Enter, Back = Esc, ` or Del, Help = ? (Shift+/)
//   Text mode:       printable characters (including ?) -> Char, Del -> Erase, Enter -> Select,
//                    Esc -> Back, arrow flags still navigate
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster {

struct KeySnapshot {
  bool up = false, down = false, left = false, right = false;
  bool enter = false, esc = false, backspace = false, del = false, tab = false, space = false;
  bool fn = false, shift = false, ctrl = false, opt = false, alt = false;
  char chars[8] = {};  // printable characters typed in this change (KeysState::word)
  uint8_t nchars = 0;
};

// True when the snapshot holds at least one pressed key or modifier.
inline bool any_key(const KeySnapshot& k) {
  return k.up || k.down || k.left || k.right || k.enter || k.esc || k.backspace || k.del || k.tab || k.space ||
         k.fn || k.shift || k.ctrl || k.opt || k.alt || k.nchars > 0;
}

enum class NavEvent : uint8_t { None, Up, Down, Left, Right, Select, Back, Cancel, Char, Erase, Tab, Home, Help };

struct NavOutput {
  NavEvent events[8] = {};
  char chars[8] = {};  // chars[i] is meaningful when events[i] == Char
  uint8_t count = 0;
};

class InputMapper {
 public:
  void set_text_mode(bool on) { text_mode_ = on; }
  bool text_mode() const { return text_mode_; }
  // Cancel, when present, is always the first event and suppresses every other event of that change.
  // Home, when present (and no Cancel), is the only event of that change.
  NavOutput map(const KeySnapshot& k) const;

 private:
  bool text_mode_ = false;
};

// Short display name of a logical action ("Up", "Home", ...), for the key tester and help screens.
const char* nav_event_name(NavEvent e);

}  // namespace oyster
