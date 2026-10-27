// Scratchpad module (Home tile "Scratchpad"): an offline calculator and a quick notepad, in the spirit of a
// whiteboard with no memory. Everything lives in RAM only: nothing is written to flash or SD, so it costs no
// storage and is wiped by a reboot (or by "Clear all"). It never touches the network, the backend or a radio,
// so it is always available and never shows "offline" - unlike the Control Deck, which needs the laptop.
//
// Pages (the module's own levels, drawn through the standard List/Text/Entry page kinds):
//   Menu (List)        Calculator / Notes / Clear all
//   CalcEntry (Entry)  type an expression; Enter evaluates, empty cancels back to the menu
//   CalcTape (Calc)    the calculator display: expression over a big result + history; Enter calculates again
//   NotesView (Text)   the notepad so far; Enter adds a line
//   NotesEntry (Entry) type one line; Enter appends it
//
// Threading: every handler here runs on the UI task (there is no worker call), so the state needs no lock.
#include <Arduino.h>

#include <cstdio>
#include <cstring>

#include "modules.h"
#include "oyster_core/calc_eval.h"

namespace oyster::modules {
namespace {

enum class Level : uint8_t { Menu, CalcEntry, CalcTape, NotesView, NotesEntry };

constexpr uint8_t kPageMenu = 0;
constexpr uint8_t kPageCalcEntry = 1;
constexpr uint8_t kPageCalcTape = 2;
constexpr uint8_t kPageNotesView = 3;
constexpr uint8_t kPageNotesEntry = 4;

constexpr uint8_t kTapeLines = 9;      // recent calculations kept, newest first (9 * 56 < PageView.body)
constexpr size_t kTapeLineLen = 56;    // "expr = result" or "expr  (reason)", clipped to fit
constexpr uint8_t kCalcExprMax = 48;   // characters the calculator entry accepts
constexpr size_t kNotesCap = 512;      // notepad buffer; oldest lines drop off the front when full
constexpr uint8_t kNoteLineMax = 63;   // characters one note line accepts
constexpr size_t kResultMax = 32;

struct MenuRow {
  const char* label;
};
const MenuRow kMenu[] = {{"Calculator"}, {"Notes"}, {"Clear all"}};
constexpr uint16_t kMenuCount = sizeof(kMenu) / sizeof(kMenu[0]);

struct State {
  Level level = Level::Menu;
  char tape[kTapeLines][kTapeLineLen] = {};
  uint8_t tape_count = 0;
  char last_result[kResultMax] = "";  // last good result, prefilled into "continue"
  bool have_result = false;
  char last_expr[kCalcExprMax + 1] = "";  // the latest calculation, shown on the calculator display
  char last_display[kResultMax] = "";     // its result, or the error text
  bool last_ok = true;
  bool calc_prefill = false;  // prefill the next calculator entry with last_result ("continue")
  uint32_t calc_count = 0;
  char notes[kNotesCap] = "";
};

State s;

// Push a line onto the calculator tape, newest at index 0. The oldest line falls off when the tape is full.
void push_tape(const char* line) {
  const uint8_t keep = s.tape_count < kTapeLines ? s.tape_count : static_cast<uint8_t>(kTapeLines - 1);
  for (uint8_t i = keep; i > 0; --i) std::memcpy(s.tape[i], s.tape[i - 1], kTapeLineLen);
  std::snprintf(s.tape[0], kTapeLineLen, "%s", line);
  if (s.tape_count < kTapeLines) ++s.tape_count;
}

// Append one line to the notepad, dropping whole lines from the front if it would not fit.
void append_note(const char* text) {
  if (text == nullptr || text[0] == '\0') return;
  size_t used = std::strlen(s.notes);
  const size_t need = std::strlen(text) + 1;  // + '\n'
  while (used + need >= kNotesCap) {
    char* nl = std::strchr(s.notes, '\n');
    if (nl == nullptr) {  // one giant line and still no room: start fresh
      s.notes[0] = '\0';
      used = 0;
      break;
    }
    const size_t shift = static_cast<size_t>(nl + 1 - s.notes);
    std::memmove(s.notes, nl + 1, used - shift + 1);  // include the NUL
    used -= shift;
  }
  std::snprintf(s.notes + used, kNotesCap - used, "%s\n", text);
}

void clear_all() {
  s.tape_count = 0;
  s.have_result = false;
  s.calc_prefill = false;
  s.calc_count = 0;
  s.last_result[0] = '\0';
  s.last_expr[0] = '\0';
  s.last_display[0] = '\0';
  s.last_ok = true;
  s.notes[0] = '\0';
}

// ---- views ----------------------------------------------------------------------------------------------

void view_menu(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = kPageMenu;
  std::snprintf(v.title, sizeof(v.title), "Scratchpad");
  v.count = kMenuCount;
  v.row = [](uint16_t i, char* out, size_t n) {
    std::snprintf(out, n, "%s", i < kMenuCount ? kMenu[i].label : "");
  };
  std::snprintf(v.left, sizeof(v.left), "Ent:Open");
}

void view_calc_entry(PageView& v) {
  v.kind = PageKind::Entry;
  v.page_id = kPageCalcEntry;
  std::snprintf(v.title, sizeof(v.title), "Calculator");
  std::snprintf(v.entry_prompt, sizeof(v.entry_prompt), "= expression");
  if (s.calc_prefill && s.have_result) {
    std::snprintf(v.entry_initial, sizeof(v.entry_initial), "%s", s.last_result);
  } else {
    v.entry_initial[0] = '\0';
  }
  v.entry_max = kCalcExprMax;
  v.entry_secret = false;
}

// The calculator display (PageKind::Calc, drawn by ui::draw_calc): the latest expression over a big
// result, with the tape as history (newest first; the UI shows the previous line under the display).
void view_calc_tape(PageView& v) {
  v.kind = PageKind::Calc;
  v.page_id = kPageCalcTape;
  std::snprintf(v.title, sizeof(v.title), "Calculator");
  std::snprintf(v.calc_expr, sizeof(v.calc_expr), "%s", s.last_expr);
  std::snprintf(v.calc_result, sizeof(v.calc_result), "%s", s.last_display);
  v.calc_ok = s.last_ok;
  size_t off = 0;
  for (uint8_t i = 0; i < s.tape_count && off < sizeof(v.body); ++i) {
    off += std::snprintf(v.body + off, sizeof(v.body) - off, "%s\n", s.tape[i]);
  }
  std::snprintf(v.left, sizeof(v.left), "Ent:New Del:Menu");
}

void view_notes(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = kPageNotesView;
  std::snprintf(v.title, sizeof(v.title), "Notes");
  if (s.notes[0] == '\0') {
    std::snprintf(v.body, sizeof(v.body), "No notes yet.\nEnter adds a line.");
  } else {
    std::snprintf(v.body, sizeof(v.body), "%s", s.notes);
  }
  std::snprintf(v.left, sizeof(v.left), "Ent:Add Del:Menu");
}

void view_notes_entry(PageView& v) {
  v.kind = PageKind::Entry;
  v.page_id = kPageNotesEntry;
  std::snprintf(v.title, sizeof(v.title), "Notes");
  std::snprintf(v.entry_prompt, sizeof(v.entry_prompt), "note");
  v.entry_initial[0] = '\0';
  v.entry_max = kNoteLineMax;
  v.entry_secret = false;
}

// ---- Ops ------------------------------------------------------------------------------------------------

bool mod_available(const char** why) {
  (void)why;
  return true;  // offline, self-contained: always available
}

void mod_open() {
  // Keep whatever is on the tape and in the notes (RAM persists until reboot or "Clear all"); just return to
  // the menu so re-entering the tile does not drop straight back into a stale entry page.
  s.level = Level::Menu;
  s.calc_prefill = false;
}

void mod_view(PageView& v) {
  switch (s.level) {
    case Level::Menu: view_menu(v); break;
    case Level::CalcEntry: view_calc_entry(v); break;
    case Level::CalcTape: view_calc_tape(v); break;
    case Level::NotesView: view_notes(v); break;
    case Level::NotesEntry: view_notes_entry(v); break;
  }
}

void mod_select(uint16_t row) {
  switch (s.level) {
    case Level::Menu:
      switch (row) {
        case 0:  // Calculator
          s.calc_prefill = false;
          s.level = Level::CalcEntry;
          break;
        case 1:  // Notes
          s.level = Level::NotesView;
          break;
        case 2:  // Clear all
          clear_all();
          toast("Cleared");
          break;
        default:
          break;
      }
      break;
    case Level::CalcTape:  // Enter on the tape: a fresh calculation, continuing from the last result
      s.calc_prefill = s.have_result;
      s.level = Level::CalcEntry;
      break;
    case Level::NotesView:  // Enter on the notes: add a line
      s.level = Level::NotesEntry;
      break;
    default:
      break;
  }
}

bool mod_back() {
  switch (s.level) {
    case Level::CalcEntry:
    case Level::CalcTape:
    case Level::NotesView:
      s.level = Level::Menu;
      return true;
    case Level::NotesEntry:
      s.level = Level::NotesView;
      return true;
    case Level::Menu:
      return false;  // let Home pop the module
  }
  return false;
}

void mod_entry(const char* text) {
  if (text == nullptr) text = "";
  if (s.level == Level::CalcEntry) {
    if (text[0] == '\0') {  // empty entry cancels back to the menu
      s.level = Level::Menu;
      return;
    }
    double value = 0.0;
    char err[24];
    char line[kTapeLineLen];
    if (calc::eval(text, &value, err, sizeof(err))) {
      char result[kResultMax];
      calc::format(value, result, sizeof(result));
      std::snprintf(line, sizeof(line), "%s = %s", text, result);
      std::snprintf(s.last_result, sizeof(s.last_result), "%s", result);
      std::snprintf(s.last_display, sizeof(s.last_display), "%s", result);
      s.last_ok = true;
      s.have_result = true;
      ++s.calc_count;
    } else {
      std::snprintf(line, sizeof(line), "%s  (%s)", text, err);
      std::snprintf(s.last_display, sizeof(s.last_display), "%s", err);
      s.last_ok = false;
      s.have_result = false;
    }
    std::snprintf(s.last_expr, sizeof(s.last_expr), "%s", text);
    push_tape(line);
    s.level = Level::CalcTape;
  } else if (s.level == Level::NotesEntry) {
    append_note(text);  // ignores an empty line
    s.level = Level::NotesView;
  }
}

void mod_tile_status(char* out, size_t n) {
  if (s.tape_count == 0 && s.notes[0] == '\0') {
    std::snprintf(out, n, "calc+notes");
  } else if (s.calc_count > 0) {
    std::snprintf(out, n, "%lu calc", static_cast<unsigned long>(s.calc_count));
  } else {
    std::snprintf(out, n, "notes");
  }
}

}  // namespace

const Ops kScratchOps = {"Scratchpad",   mod_available, mod_open, mod_view,       mod_select, mod_back,
                         nullptr,        nullptr,       nullptr,  nullptr,        mod_tile_status,
                         mod_entry};

}  // namespace oyster::modules
