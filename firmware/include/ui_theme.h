// OysterOS design tokens as compile-time constants: palette (docs/DESIGN_SYSTEM.md "Design tokens",
// Project Plan 11.3), spatial sizing / layout anatomy (DESIGN_SYSTEM.md "Spatial sizing", Plan 11.1) and
// type sizes. Colours are RGB565 for the 16-bit band sprite of src/ui/widgets.cpp. Pure constants: no
// M5 headers, usable from host code. Sizes are not yet checked against photos of the real display.
#pragma once

#include <cstdint>

namespace oyster::theme {

// 0xRRGGBB -> RGB565.
constexpr uint16_t rgb565(uint32_t rgb) {
  return static_cast<uint16_t>(((rgb >> 8) & 0xF800u) | ((rgb >> 5) & 0x07E0u) | ((rgb >> 3) & 0x001Fu));
}

// ---- palette (token name in the design docs -> use) -----------------------------------------------
constexpr uint16_t kSurfaceBase = rgb565(0x101923);   // Background / surface.base: shell, margins, bars
constexpr uint16_t kSurfacePanel = rgb565(0x192C3D);  // Panel / surface.panel: lists and content
constexpr uint16_t kSurfaceFocus = rgb565(0x234B54);  // Focus / surface.focus: focused row or tile
constexpr uint16_t kAccent = rgb565(0x5EEAD4);        // Accent: selection stripe, connected, live radio
constexpr uint16_t kTextPrimary = rgb565(0xF4FAFF);   // Text / text.primary
constexpr uint16_t kTextMuted = rgb565(0xA9C4CE);     // Secondary / text.muted: labels, disabled entries
constexpr uint16_t kWarning = rgb565(0xE9AD52);       // Warning: confirmations, waiting
constexpr uint16_t kError = rgb565(0xF17A7A);         // Error: problems, alert headings

static_assert(rgb565(0xFFFFFF) == 0xFFFF && rgb565(0x000000) == 0x0000, "rgb565 conversion");
static_assert(rgb565(0xFF0000) == 0xF800 && rgb565(0x00FF00) == 0x07E0 && rgb565(0x0000FF) == 0x001F,
              "rgb565 channel order");

// ---- layout anatomy, 240 x 135 landscape -------------------------------------------------------------
constexpr int kScreenW = 240;
constexpr int kScreenH = 135;
constexpr int kPadX = 5;                          // safe horizontal padding
constexpr int kInnerW = kScreenW - 2 * kPadX;     // 230: panel width
constexpr int kStatusY = 0, kStatusH = 15;        // status bar y 0..14 (y 15: divider)
constexpr int kHeadingY = 16, kHeadingH = 16;     // heading y 16..31
constexpr int kContentY = 33, kContentH = 84;     // content y 33..116
constexpr int kFooterY = 119, kFooterH = 16;      // action strip y 119..134
constexpr int kBandH = 16;                        // height of the band sprite every region is painted with
constexpr int kStripeW = 3;                       // accent stripe of the selected row / tile
constexpr int kRowH = 16;                         // list and text rows
constexpr int kRows = 5;                          // rows in the content area (design: 3-5 actionable rows)
constexpr int kRowsY = kContentY + 1;             // rows y 34..113; 1 px panel pad above, 3 px below
constexpr int kTileH = 28;                        // icon rows (24 px icon + 2 px above and below)
constexpr int kTileRows = 3;                      // 3 x 28 = the whole content area
constexpr int kScrollX = kScreenW - kPadX + 1;    // 3 px scrollbar in the right margin, x 236..238
constexpr int kScrollW = 3;

static_assert(kStatusY + kStatusH < kHeadingY, "status bar overlaps heading");
static_assert(kHeadingY + kHeadingH < kContentY, "heading overlaps content");
static_assert(kRowsY + kRows * kRowH <= kContentY + kContentH, "rows overflow the content area");
static_assert(kTileRows * kTileH == kContentH, "tiles must fill the content area");
static_assert(kContentY + kContentH < kFooterY && kFooterY + kFooterH == kScreenH, "footer position");
static_assert(kStatusH < kBandH && kHeadingH == kBandH && kFooterH == kBandH && kRowH == kBandH, "band size");

// ---- type ------------------------------------------------------------------------------------------
// Body, headings and key hints: AsciiFont8x16 (fixed 8 x 16 cell, ~12 px glyphs; headings are drawn twice
// 1 px apart as the bold heading style). Status bar and overflow notices: Font0 (fixed 6 x 8 cell). The
// design asks for 14 px bold headings and >= 10 px metadata: to be judged on photos of the device.
constexpr int kBodyW = 8, kBodyH = 16;
constexpr int kMetaW = 6, kMetaH = 8;
constexpr int kListTextX = kPadX + kStripeW + kBodyW;    // 16: after the stripe and the '>' marker
constexpr int kListCols = (kInnerW - kStripeW - kBodyW - 3) / kBodyW;  // 27
constexpr int kTextX = kPadX + 4;                        // 9: detail / message text
constexpr int kTextCols = (kInnerW - 6) / kBodyW;        // 28
constexpr int kFooterCols = kInnerW / kBodyW;            // 28

static_assert(kListTextX + kListCols * kBodyW <= kPadX + kInnerW, "list text overflows the panel");
static_assert(kTextX + kTextCols * kBodyW <= kPadX + kInnerW, "message text overflows the panel");

}  // namespace oyster::theme
