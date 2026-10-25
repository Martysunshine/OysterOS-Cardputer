// UI primitives for the 240x135 LCD in the OysterOS design system (docs/DESIGN_SYSTEM.md, tokens and
// layout in ui_theme.h). Regions: status bar y0..14, heading y16..31, content y33..116 (5 rows of 16 px or
// 3 icon rows of 28 px), action strip (footer) y119..134. Everything is painted through ONE resident
// 240x16 RGB565 band sprite (7,680 B of internal RAM, no full-screen framebuffer); the caller redraws a
// region only when it changed, nothing animates on its own. Implementation: src/ui/*.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

#include "assets.h"
#include "ui_theme.h"

namespace oyster {

enum class ModeBanner : uint8_t;

namespace ui {

constexpr int kW = theme::kScreenW, kH = theme::kScreenH;
constexpr int kRows = theme::kRows;            // list / message rows visible in the content area
constexpr int kTextCols = theme::kTextCols;    // message and text-entry columns (word wrap width)
constexpr int kTileRows = theme::kTileRows;    // icon rows visible in the content area

void begin();  // allocate the band sprite
bool ready();  // false when the sprite could not be allocated (every draw call is then a no-op)

// Connection icon of the status bar (derived from the real backend state by the caller).
enum class Conn : uint8_t { Unknown, Offline, Connecting, Online, Error };

// Status bar: mode banner + session short id + connection dot + backend state + battery.
void draw_status(ModeBanner mode, const char* session_short, const char* backend, int battery_pct,
                 Conn conn = Conn::Unknown);

// Paginated list (MenuList / FocusRow): heading `title` with "sel/count", then kRows rows from `top`.
// The selected row has the accent stripe, the '>' marker and the focus background.
struct ListModel {
  const char* title;                              // leading '!' = alert heading
  uint16_t count;
  uint16_t selected;
  uint16_t top;                                   // first visible row
  void (*row_text)(uint16_t i, char* out, size_t n);
  bool (*row_disabled)(uint16_t i) = nullptr;     // muted text (unavailable entry)
  const char* empty = nullptr;                    // shown when count == 0 (default "(empty)")
};
void draw_list(const ListModel& m);

// Icon tiles (Home grid, module menus): 24 px icon, label and a one-line status in 1 or 2 columns.
struct Tile {
  assets::IconId icon; // IconId::Count = no bitmap (an outlined '?' glyph is drawn)
  const char* label;   // clipped to the tile
  const char* detail;  // small second line (status), may be nullptr
  bool enabled;        // false: muted label and dimmed icon (unavailable module)
};
struct TileModel {
  const char* title;   // leading '!' = alert heading
  const char* right;   // heading right text (position), may be nullptr
  const Tile* tiles;   // row-major
  uint8_t count;
  uint8_t columns;     // 1 or 2
  uint8_t selected;
  uint8_t top_row;     // first visible tile row
};
void draw_tiles(const TileModel& m);

// Action strip with key hints "Key:Action Key:Action" (key part in the accent colour). Leading '!' in
// `left` = alert style. Uses the 8x16 font when it fits, else the 6x8 font (two lines if needed).
void draw_footer(const char* left, const char* right);
// Notification in the action strip (toast): never truncated up to 72 characters (two small lines).
void draw_notice(const char* text);

// Progress for a long operation: label, elapsed seconds, item count, activity bar, cancel hint.
void draw_progress(const char* label, uint32_t elapsed_s, uint32_t items, bool cancellable);

// Modal confirm: operation / target / expected effect. Keys are shown by the caller's footer.
void draw_confirm(const char* operation, const char* target, const char* effect);

// Scrollable text / detail view. `body` starts at wrapped line `first_line` of a text of `total_lines`
// lines (both 0 = no scrollbar).
void draw_message(const char* title, const char* body, uint16_t first_line = 0, uint16_t total_lines = 0);
uint16_t wrapped_line_count(const char* body);                 // lines body occupies at kTextCols
const char* skip_wrapped_lines(const char* body, uint16_t n);  // start of wrapped line n (for scrolling)
// Text entry: 4 text rows with a block caret, then `hint` (default: the character count).
void draw_text_entry(const char* prompt, const char* buffer, size_t cursor, const char* hint = nullptr);

// Flash images (assets.h) drawn straight to the screen; transparent pixels show `bg`. `dim` blends the
// image towards `bg` (disabled state).
void draw_icon(assets::IconId id, int x, int y, uint16_t bg, bool dim = false);
void draw_image(const assets::Image& img, int x, int y, uint16_t bg, bool dim = false);
// Draws one opaque frame of a loader animation at its own position, straight from flash (no RAM buffer).
void draw_animation_frame(const assets::Animation& anim, uint8_t frame);

}  // namespace ui
}  // namespace oyster
