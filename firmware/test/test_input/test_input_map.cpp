// Host tests for InputMapper (oyster_core/input_map.h). Run: pio test -e native -f test_input
#include <unity.h>

#include <cstring>

#include "oyster_core/input_map.h"

using namespace oyster;

namespace {

KeySnapshot chars(const char* s) {
  KeySnapshot k;
  const size_t n = std::strlen(s);
  for (size_t i = 0; i < n && i < sizeof(k.chars); ++i) k.chars[k.nchars++] = s[i];
  return k;
}

NavOutput map_in(bool text_mode, const KeySnapshot& k) {
  InputMapper m;
  m.set_text_mode(text_mode);
  return m.map(k);
}

void assert_single(const NavOutput& o, NavEvent e) {
  TEST_ASSERT_EQUAL_UINT8(1, o.count);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(e), static_cast<uint8_t>(o.events[0]));
}

bool contains(const NavOutput& o, NavEvent e) {
  for (uint8_t i = 0; i < o.count; ++i) {
    if (o.events[i] == e) return true;
  }
  return false;
}

// A busy key change: Enter, an arrow, Backspace, Tab and printable characters together.
KeySnapshot busy_change(const char* s) {
  KeySnapshot k = chars(s);
  k.enter = true;
  k.down = true;
  k.backspace = true;
  k.tab = true;
  return k;
}

}  // namespace

void setUp() {}
void tearDown() {}

// ---- cancel ---------------------------------------------------------------------------------

void test_fn_esc_cancels_and_suppresses_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot k = busy_change("ab;");
    k.fn = true;
    k.esc = true;
    assert_single(map_in(mode == 1, k), NavEvent::Cancel);
  }
}

void test_fn_backtick_cancels_and_suppresses_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot k = busy_change("x`");
    k.fn = true;
    assert_single(map_in(mode == 1, k), NavEvent::Cancel);
    KeySnapshot shifted = chars("~");  // Fn+Shift+` reports '~'
    shifted.fn = true;
    shifted.shift = true;
    assert_single(map_in(mode == 1, shifted), NavEvent::Cancel);
  }
}

void test_ctrl_c_cancels_and_suppresses_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot upper = busy_change("C");  // the library reports the shifted legend under Ctrl
    upper.ctrl = true;
    assert_single(map_in(mode == 1, upper), NavEvent::Cancel);
    KeySnapshot lower = chars("c");
    lower.ctrl = true;
    assert_single(map_in(mode == 1, lower), NavEvent::Cancel);
  }
}

void test_cancel_is_first_event_even_with_full_char_buffer() {
  KeySnapshot k = chars("abcdefg`");
  k.fn = true;
  NavOutput o = map_in(true, k);
  assert_single(o, NavEvent::Cancel);
}

void test_partial_combos_do_not_cancel() {
  // Bare c, bare `, Ctrl alone, Fn alone, Esc alone and Ctrl+X are not cancel.
  TEST_ASSERT_FALSE(contains(map_in(false, chars("c")), NavEvent::Cancel));
  TEST_ASSERT_FALSE(contains(map_in(true, chars("c")), NavEvent::Cancel));
  TEST_ASSERT_FALSE(contains(map_in(false, chars("`")), NavEvent::Cancel));
  KeySnapshot ctrl;
  ctrl.ctrl = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(false, ctrl).count);
  KeySnapshot fn;
  fn.fn = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, fn).count);
  KeySnapshot esc;
  esc.esc = true;
  assert_single(map_in(false, esc), NavEvent::Back);
  KeySnapshot ctrl_x = chars("X");
  ctrl_x.ctrl = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, ctrl_x).count);
}

// ---- navigation mode ------------------------------------------------------------------------

void test_bare_arrow_legends_navigate_in_nav_mode() {
  assert_single(map_in(false, chars(";")), NavEvent::Up);
  assert_single(map_in(false, chars(".")), NavEvent::Down);
  assert_single(map_in(false, chars(",")), NavEvent::Left);
  assert_single(map_in(false, chars("/")), NavEvent::Right);
}

void test_bare_arrow_legends_are_text_in_text_mode() {
  const char* legends = ";.,/";
  for (int i = 0; i < 4; ++i) {
    char s[2] = {legends[i], 0};
    NavOutput o = map_in(true, chars(s));
    assert_single(o, NavEvent::Char);
    TEST_ASSERT_EQUAL_CHAR(legends[i], o.chars[0]);
  }
}

void test_fn_arrow_legends_navigate_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot up = chars(";");
    up.fn = true;
    assert_single(map_in(mode == 1, up), NavEvent::Up);
    KeySnapshot right = chars("/");
    right.fn = true;
    assert_single(map_in(mode == 1, right), NavEvent::Right);
  }
}

void test_arrow_flags_navigate_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot k;
    k.up = true;
    assert_single(map_in(mode == 1, k), NavEvent::Up);
    KeySnapshot d;
    d.down = true;
    assert_single(map_in(mode == 1, d), NavEvent::Down);
    KeySnapshot l;
    l.left = true;
    assert_single(map_in(mode == 1, l), NavEvent::Left);
    KeySnapshot r;
    r.right = true;
    assert_single(map_in(mode == 1, r), NavEvent::Right);
  }
}

void test_esc_backtick_backspace_are_back_in_nav_mode() {
  KeySnapshot esc;
  esc.esc = true;
  assert_single(map_in(false, esc), NavEvent::Back);
  assert_single(map_in(false, chars("`")), NavEvent::Back);
  KeySnapshot bs;
  bs.backspace = true;
  assert_single(map_in(false, bs), NavEvent::Back);
  KeySnapshot del;
  del.del = true;
  assert_single(map_in(false, del), NavEvent::Back);
}

void test_back_is_reported_once_per_change() {
  KeySnapshot k = chars("`");
  k.esc = true;
  k.backspace = true;
  assert_single(map_in(false, k), NavEvent::Back);
}

void test_enter_selects_and_tab_in_nav_mode() {
  KeySnapshot k;
  k.enter = true;
  assert_single(map_in(false, k), NavEvent::Select);
  KeySnapshot t;
  t.tab = true;
  assert_single(map_in(false, t), NavEvent::Tab);
}

void test_other_printables_are_hotkeys_in_nav_mode() {
  NavOutput o = map_in(false, chars("s1"));
  TEST_ASSERT_EQUAL_UINT8(2, o.count);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(NavEvent::Char), static_cast<uint8_t>(o.events[0]));
  TEST_ASSERT_EQUAL_CHAR('s', o.chars[0]);
  TEST_ASSERT_EQUAL_CHAR('1', o.chars[1]);
}

// ---- text mode ------------------------------------------------------------------------------

void test_text_mode_chars_in_order() {
  NavOutput o = map_in(true, chars("Hi 7?"));
  TEST_ASSERT_EQUAL_UINT8(5, o.count);
  const char expect[] = "Hi 7?";
  for (uint8_t i = 0; i < 5; ++i) {
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(NavEvent::Char), static_cast<uint8_t>(o.events[i]));
    TEST_ASSERT_EQUAL_CHAR(expect[i], o.chars[i]);
  }
}

void test_text_mode_backtick_is_a_character() {
  NavOutput o = map_in(true, chars("`"));
  assert_single(o, NavEvent::Char);
  TEST_ASSERT_EQUAL_CHAR('`', o.chars[0]);
}

void test_text_mode_backspace_and_del_erase() {
  KeySnapshot bs;
  bs.backspace = true;
  assert_single(map_in(true, bs), NavEvent::Erase);
  KeySnapshot del;
  del.del = true;
  assert_single(map_in(true, del), NavEvent::Erase);
}

void test_text_mode_enter_selects_and_esc_backs() {
  KeySnapshot k;
  k.enter = true;
  assert_single(map_in(true, k), NavEvent::Select);
  KeySnapshot esc;
  esc.esc = true;
  assert_single(map_in(true, esc), NavEvent::Back);
}

void test_text_mode_ignores_modifier_chords_and_nonprintables() {
  KeySnapshot alt = chars("a");
  alt.alt = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, alt).count);
  KeySnapshot fn = chars("q");
  fn.fn = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, fn).count);
  KeySnapshot ctl = chars("\x01\x7f");
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, ctl).count);
}

void test_nchars_out_of_range_is_bounded() {
  KeySnapshot k = chars("abcdefgh");
  k.nchars = 200;  // corrupt count must not read past the buffer
  NavOutput o = map_in(true, k);
  TEST_ASSERT_EQUAL_UINT8(8, o.count);
  TEST_ASSERT_EQUAL_CHAR('h', o.chars[7]);
}

void test_empty_change_maps_to_nothing() {
  KeySnapshot k;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(false, k).count);
  TEST_ASSERT_EQUAL_UINT8(0, map_in(true, k).count);
}

void test_mode_toggle() {
  InputMapper m;
  TEST_ASSERT_FALSE(m.text_mode());
  m.set_text_mode(true);
  TEST_ASSERT_TRUE(m.text_mode());
  m.set_text_mode(false);
  assert_single(m.map(chars(";")), NavEvent::Up);
}

// ---- Home and Help (proposed bindings, UNVERIFIED on hardware) --------------------------------

void test_fn_del_is_home_in_both_modes() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot bs;
    bs.fn = true;
    bs.backspace = true;
    assert_single(map_in(mode == 1, bs), NavEvent::Home);
    KeySnapshot del;
    del.fn = true;
    del.del = true;
    assert_single(map_in(mode == 1, del), NavEvent::Home);
  }
}

void test_home_suppresses_other_events_of_the_change() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot k = busy_change("ab1?");  // Enter, Down, Backspace, Tab and characters
    k.fn = true;
    assert_single(map_in(mode == 1, k), NavEvent::Home);
  }
}

void test_cancel_beats_home() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot k = chars("`");
    k.fn = true;
    k.backspace = true;
    assert_single(map_in(mode == 1, k), NavEvent::Cancel);
    KeySnapshot c = chars("C");
    c.ctrl = true;
    c.fn = true;
    c.del = true;
    assert_single(map_in(mode == 1, c), NavEvent::Cancel);
  }
}

void test_bare_del_is_not_home() {
  KeySnapshot k;
  k.backspace = true;
  TEST_ASSERT_FALSE(contains(map_in(false, k), NavEvent::Home));
  TEST_ASSERT_FALSE(contains(map_in(true, k), NavEvent::Home));
  KeySnapshot fn_only;  // Fn with a non-arrow character is reserved, not Home
  fn_only = chars("h");
  fn_only.fn = true;
  TEST_ASSERT_EQUAL_UINT8(0, map_in(false, fn_only).count);
}

void test_question_mark_is_help_in_nav_mode() {
  KeySnapshot k = chars("?");
  k.shift = true;  // Shift+/ reports the '?' legend
  assert_single(map_in(false, k), NavEvent::Help);
  assert_single(map_in(false, chars("??")), NavEvent::Help);  // reported once per change
}

void test_question_mark_is_text_in_text_mode() {
  KeySnapshot k = chars("?");
  k.shift = true;
  NavOutput o = map_in(true, k);
  assert_single(o, NavEvent::Char);
  TEST_ASSERT_EQUAL_CHAR('?', o.chars[0]);
}

void test_help_ignores_modifier_and_fn_chords() {
  for (int mode = 0; mode < 2; ++mode) {
    KeySnapshot fn = chars("?");
    fn.fn = true;
    TEST_ASSERT_EQUAL_UINT8(0, map_in(mode == 1, fn).count);
    KeySnapshot ctl = chars("?");
    ctl.ctrl = true;
    TEST_ASSERT_EQUAL_UINT8(0, map_in(mode == 1, ctl).count);
  }
}

void test_help_keeps_other_hotkeys() {
  NavOutput o = map_in(false, chars("1?"));
  TEST_ASSERT_EQUAL_UINT8(2, o.count);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(NavEvent::Char), static_cast<uint8_t>(o.events[0]));
  TEST_ASSERT_EQUAL_CHAR('1', o.chars[0]);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(NavEvent::Help), static_cast<uint8_t>(o.events[1]));
}

void test_nav_event_names_are_distinct() {
  const NavEvent all[] = {NavEvent::None,   NavEvent::Up,   NavEvent::Down,  NavEvent::Left, NavEvent::Right,
                          NavEvent::Select, NavEvent::Back, NavEvent::Cancel, NavEvent::Char, NavEvent::Erase,
                          NavEvent::Tab,    NavEvent::Home, NavEvent::Help};
  const size_t n = sizeof(all) / sizeof(all[0]);
  for (size_t i = 0; i < n; ++i) {
    const char* a = nav_event_name(all[i]);
    TEST_ASSERT_NOT_NULL(a);
    TEST_ASSERT_TRUE(a[0] != '\0');
    TEST_ASSERT_TRUE(std::strcmp(a, "?") != 0);
    for (size_t j = i + 1; j < n; ++j) TEST_ASSERT_TRUE(std::strcmp(a, nav_event_name(all[j])) != 0);
  }
  TEST_ASSERT_EQUAL_STRING("Home", nav_event_name(NavEvent::Home));
  TEST_ASSERT_EQUAL_STRING("Help", nav_event_name(NavEvent::Help));
  TEST_ASSERT_EQUAL_STRING("?", nav_event_name(static_cast<NavEvent>(200)));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_fn_esc_cancels_and_suppresses_in_both_modes);
  RUN_TEST(test_fn_backtick_cancels_and_suppresses_in_both_modes);
  RUN_TEST(test_ctrl_c_cancels_and_suppresses_in_both_modes);
  RUN_TEST(test_cancel_is_first_event_even_with_full_char_buffer);
  RUN_TEST(test_partial_combos_do_not_cancel);
  RUN_TEST(test_bare_arrow_legends_navigate_in_nav_mode);
  RUN_TEST(test_bare_arrow_legends_are_text_in_text_mode);
  RUN_TEST(test_fn_arrow_legends_navigate_in_both_modes);
  RUN_TEST(test_arrow_flags_navigate_in_both_modes);
  RUN_TEST(test_esc_backtick_backspace_are_back_in_nav_mode);
  RUN_TEST(test_back_is_reported_once_per_change);
  RUN_TEST(test_enter_selects_and_tab_in_nav_mode);
  RUN_TEST(test_other_printables_are_hotkeys_in_nav_mode);
  RUN_TEST(test_text_mode_chars_in_order);
  RUN_TEST(test_text_mode_backtick_is_a_character);
  RUN_TEST(test_text_mode_backspace_and_del_erase);
  RUN_TEST(test_text_mode_enter_selects_and_esc_backs);
  RUN_TEST(test_text_mode_ignores_modifier_chords_and_nonprintables);
  RUN_TEST(test_nchars_out_of_range_is_bounded);
  RUN_TEST(test_empty_change_maps_to_nothing);
  RUN_TEST(test_mode_toggle);
  RUN_TEST(test_fn_del_is_home_in_both_modes);
  RUN_TEST(test_home_suppresses_other_events_of_the_change);
  RUN_TEST(test_cancel_beats_home);
  RUN_TEST(test_bare_del_is_not_home);
  RUN_TEST(test_question_mark_is_help_in_nav_mode);
  RUN_TEST(test_question_mark_is_text_in_text_mode);
  RUN_TEST(test_help_ignores_modifier_and_fn_chords);
  RUN_TEST(test_help_keeps_other_hotkeys);
  RUN_TEST(test_nav_event_names_are_distinct);
  return UNITY_END();
}
