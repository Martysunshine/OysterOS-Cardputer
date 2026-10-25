// Pure keyboard-to-navigation mapping. See input_map.h for the proposed mapping (to be confirmed on hardware).
#include "oyster_core/input_map.h"

namespace oyster {

namespace {

constexpr uint8_t kMaxEvents = sizeof(NavOutput::events) / sizeof(NavOutput::events[0]);

bool has_char(const KeySnapshot& k, char c) {
  const uint8_t n = k.nchars < sizeof(k.chars) ? k.nchars : sizeof(k.chars);
  for (uint8_t i = 0; i < n; ++i) {
    if (k.chars[i] == c) return true;
  }
  return false;
}

bool is_printable(char c) { return c >= 0x20 && c <= 0x7E; }

// Non-character events are emitted at most once per change; characters may repeat.
void push(NavOutput& out, NavEvent e, char c = 0) {
  if (out.count >= kMaxEvents) return;
  if (e != NavEvent::Char) {
    for (uint8_t i = 0; i < out.count; ++i) {
      if (out.events[i] == e) return;
    }
  }
  out.events[out.count] = e;
  out.chars[out.count] = (e == NavEvent::Char) ? c : 0;
  ++out.count;
}

// ; . , / are the arrow legends of the Cardputer keyboard (Fn layer).
NavEvent arrow_for(char c) {
  switch (c) {
    case ';': return NavEvent::Up;
    case '.': return NavEvent::Down;
    case ',': return NavEvent::Left;
    case '/': return NavEvent::Right;
    default: return NavEvent::None;
  }
}

bool is_cancel(const KeySnapshot& k) {
  if (k.fn && k.esc) return true;                                   // Fn+Esc
  if (k.fn && (has_char(k, '`') || has_char(k, '~'))) return true;  // Fn+` (Esc legend)
  if (k.ctrl && (has_char(k, 'c') || has_char(k, 'C'))) return true;  // Ctrl+C (library reports 'C' under Ctrl)
  return false;
}

// Proposed Home chord (UNVERIFIED): Fn+Del. A bare Del stays Back / Erase.
bool is_home(const KeySnapshot& k) { return k.fn && (k.backspace || k.del); }

}  // namespace

NavOutput InputMapper::map(const KeySnapshot& k) const {
  NavOutput out;

  if (is_cancel(k)) {
    push(out, NavEvent::Cancel);
    return out;  // suppresses every other event of this change
  }
  if (is_home(k)) {
    push(out, NavEvent::Home);
    return out;  // never combined with Back/Erase or a selection in the same change
  }

  if (k.up) push(out, NavEvent::Up);
  if (k.down) push(out, NavEvent::Down);
  if (k.left) push(out, NavEvent::Left);
  if (k.right) push(out, NavEvent::Right);
  if (k.enter) push(out, NavEvent::Select);
  if (k.esc) push(out, NavEvent::Back);
  if (k.tab) push(out, NavEvent::Tab);

  if (k.backspace || k.del) push(out, text_mode_ ? NavEvent::Erase : NavEvent::Back);

  const uint8_t n = k.nchars < sizeof(k.chars) ? k.nchars : sizeof(k.chars);
  for (uint8_t i = 0; i < n; ++i) {
    const char c = k.chars[i];
    const NavEvent arrow = arrow_for(c);
    if (k.fn) {
      // Fn layer: arrows in both modes; other Fn chords are reserved and ignored.
      if (arrow != NavEvent::None) push(out, arrow);
      continue;
    }
    if (k.ctrl || k.opt || k.alt) continue;  // modifier chords are never text or navigation
    if (!text_mode_) {
      if (arrow != NavEvent::None) {
        push(out, arrow);
        continue;
      }
      if (c == '`') {
        push(out, NavEvent::Back);
        continue;
      }
      if (c == '?') {  // Shift+/ (proposed Help, UNVERIFIED); a plain character in text mode
        push(out, NavEvent::Help);
        continue;
      }
    }
    if (is_printable(c)) push(out, NavEvent::Char, c);  // text in text mode, hotkeys in nav mode
  }
  return out;
}

const char* nav_event_name(NavEvent e) {
  switch (e) {
    case NavEvent::None: return "None";
    case NavEvent::Up: return "Up";
    case NavEvent::Down: return "Down";
    case NavEvent::Left: return "Left";
    case NavEvent::Right: return "Right";
    case NavEvent::Select: return "Select";
    case NavEvent::Back: return "Back";
    case NavEvent::Cancel: return "Cancel";
    case NavEvent::Char: return "Char";
    case NavEvent::Erase: return "Erase";
    case NavEvent::Tab: return "Tab";
    case NavEvent::Home: return "Home";
    case NavEvent::Help: return "Help";
  }
  return "?";
}

}  // namespace oyster
