// UI primitives (ui.h) in the OysterOS design system (ui_theme.h).
// One resident 240x16 RGB565 sprite (7,680 B of internal RAM; the ADV has no PSRAM) paints every region:
// each status bar, heading, row, tile band and footer is drawn into it and pushed before the next one.
// pushSprite() ends its SPI transaction (and waits for it) before returning, so the sprite is reused at once.
// RGB565 instead of the former three 8-bit sprites (10,800 B): RGB332 cannot show the palette (the panel
// #192C3D would become dark green and the shell #101923 black).
// Fonts: AsciiFont8x16 for headings (drawn twice 1 px apart = bold), rows and key hints; Font0 (6x8) for
// the status bar, tile status lines and notices too long for the 8x16 font.
// Conventions: a leading '!' in a title or in the footer `left` text selects the alert style.
#include "ui.h"

#include <M5Cardputer.h>

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "app_controller.h"

namespace oyster::ui {

namespace {

using namespace theme;

constexpr int kSmallCols = kInnerW / kMetaW;  // 38

M5Canvas s_band;
bool s_ready = false;

enum class Tone : uint8_t { Normal, Alert, Warn };

struct Scroll {
  uint32_t total;    // rows / lines / tile rows in the whole content
  uint32_t visible;  // how many fit on screen
  uint32_t first;    // first visible one
};

bool make(M5Canvas& c, int w, int h) {
  c.setPsram(false);  // internal DMA-capable RAM only
  c.setColorDepth(16);
  if (c.createSprite(w, h) == nullptr) return false;
  c.setTextWrap(false);
  c.setTextDatum(lgfx::textdatum_t::top_left);
  return true;
}

// Printable-ASCII copy of at most `cols` characters; '~' marks truncation.
void fit(const char* in, size_t in_len, char* out, int cols) {
  int n = 0;
  for (size_t i = 0; i < in_len && in[i] != '\0'; ++i) {
    if (n >= cols) {
      out[cols - 1] = '~';
      break;
    }
    const char c = in[i];
    out[n++] = (c >= 0x20 && c <= 0x7E) ? c : '?';
  }
  out[n < cols ? n : cols] = '\0';
}

// Word-wraps the next line of `s` into `out` (<= cols characters). Returns the number of input bytes
// consumed, including the separator. 0 means the input is exhausted.
size_t wrap_next(const char* s, int cols, char* out) {
  out[0] = '\0';
  if (s == nullptr || *s == '\0') return 0;
  int n = 0;
  int last_space = -1;
  while (s[n] != '\0' && s[n] != '\n' && n < cols) {
    if (s[n] == ' ') last_space = n;
    ++n;
  }
  size_t take = static_cast<size_t>(n);
  size_t consumed = take;
  if (s[n] == '\n') {
    consumed = take + 1;
  } else if (s[n] != '\0' && s[n] != ' ' && last_space > 0) {
    take = static_cast<size_t>(last_space);  // break before the word that does not fit
    consumed = take + 1;
  } else if (s[n] == ' ') {
    consumed = take + 1;
  }
  fit(s, take, out, cols);
  return consumed;
}

// ---- band primitives ------------------------------------------------------------------------------------

void band_fill(uint16_t c) { s_band.fillSprite(c); }
void band_push(int y) { s_band.pushSprite(&M5Cardputer.Display, 0, y); }
void band_panel(uint16_t c) { s_band.fillRect(kPadX, 0, kInnerW, kBandH, c); }

// Text with a transparent background (the band is filled first).
void text(const char* s, int x, int y, uint16_t fg, bool small = false) {
  s_band.setFont(small ? static_cast<const lgfx::IFont*>(&fonts::Font0)
                       : static_cast<const lgfx::IFont*>(&fonts::AsciiFont8x16));
  s_band.setTextColor(fg);
  s_band.drawString(s, x, y);
}

// The part of a scrollbar for the region [region_y, region_y + region_h) that falls into the band at band_y.
void scrollbar(const Scroll& s, int region_y, int region_h, int band_y) {
  if (s.visible == 0 || s.total <= s.visible) return;
  const int y = region_y - band_y;
  s_band.fillRect(kScrollX, y, kScrollW, region_h, kSurfacePanel);
  int th = static_cast<int>(static_cast<uint32_t>(region_h) * s.visible / s.total);
  if (th < 6) th = 6;
  const uint32_t span = s.total - s.visible;
  const uint32_t first = s.first < span ? s.first : span;
  const int ty = static_cast<int>(static_cast<uint32_t>(region_h - th) * first / span);
  s_band.fillRect(kScrollX, y + ty, kScrollW, th, kAccent);
}

// Full-width rows of the content area that no band covers: shell margins + panel.
void pad_rows(int y, int h) {
  if (h <= 0) return;
  auto& d = M5Cardputer.Display;
  d.fillRect(0, y, kPadX, h, kSurfaceBase);
  d.fillRect(kPadX, y, kInnerW, h, kSurfacePanel);
  d.fillRect(kPadX + kInnerW, y, kScreenW - kPadX - kInnerW, h, kSurfaceBase);
}

void content_pads(bool row_layout) {
  auto& d = M5Cardputer.Display;
  const int gap_y = kHeadingY + kHeadingH;  // y 32
  d.fillRect(0, gap_y, kScreenW, kContentY - gap_y, kSurfaceBase);
  if (row_layout) {
    pad_rows(kContentY, kRowsY - kContentY);
    const int below = kRowsY + kRows * kRowH;
    pad_rows(below, kContentY + kContentH - below);
  }
  const int end = kContentY + kContentH;  // y 117..118
  d.fillRect(0, end, kScreenW, kFooterY - end, kSurfaceBase);
}

// ---- heading ----------------------------------------------------------------------------------------------

void heading_tone(const char* t, const char* right, Tone tone) {
  const uint16_t bg = tone == Tone::Alert ? kError : (tone == Tone::Warn ? kWarning : kSurfaceBase);
  const uint16_t fg = tone == Tone::Normal ? kTextPrimary : kSurfaceBase;
  band_fill(bg);
  int right_x = kPadX + kInnerW;
  if (right && right[0]) {
    char rbuf[13];
    fit(right, SIZE_MAX, rbuf, 12);
    right_x -= static_cast<int>(std::strlen(rbuf)) * kBodyW;
    text(rbuf, right_x, 0, tone == Tone::Normal ? kTextMuted : fg);
  }
  const int cols = (right_x - kPadX - 1 - kBodyW) / kBodyW;  // one column gap + 1 px for the bold pass
  char buf[kFooterCols + 1];
  fit(t ? t : "", SIZE_MAX, buf, cols > 0 ? cols : 1);
  text(buf, kPadX, 0, fg);
  text(buf, kPadX + 1, 0, fg);
  band_push(kHeadingY);
}

void heading(const char* title, const char* right) {
  const char* t = title ? title : "";
  if (t[0] == '!') {
    heading_tone(t + 1, right, Tone::Alert);
  } else {
    heading_tone(t, right, Tone::Normal);
  }
}

// ---- content rows ------------------------------------------------------------------------------------------

// Row background: panel inside the margins. The selected row gets the focus background, the accent stripe
// and the '>' marker (focus is never shown by colour alone). `warn_stripe` marks rows of a modal dialog.
void row_begin(bool selected, bool warn_stripe = false) {
  band_fill(kSurfaceBase);
  band_panel(selected ? kSurfaceFocus : kSurfacePanel);
  if (selected) {
    s_band.fillRect(kPadX, 0, kStripeW, kRowH, kAccent);
    text(">", kPadX + kStripeW, 0, kAccent);
  } else if (warn_stripe) {
    s_band.fillRect(kPadX, 0, kStripeW, kRowH, kWarning);
  }
}

void row_push(int row, const Scroll* sc) {
  const int y = kRowsY + row * kRowH;
  if (sc) scrollbar(*sc, kRowsY, kRows * kRowH, y);
  band_push(y);
}

void list_row(int row, const char* s, bool selected, uint16_t fg, const Scroll* sc) {
  row_begin(selected);
  char buf[kListCols + 1];
  fit(s ? s : "", SIZE_MAX, buf, kListCols);
  text(buf, kListTextX, 0, fg);
  row_push(row, sc);
}

void text_row(int row, const char* s, uint16_t fg, const Scroll* sc, bool warn_stripe = false) {
  row_begin(false, warn_stripe);
  char buf[kTextCols + 1];
  fit(s ? s : "", SIZE_MAX, buf, kTextCols);
  text(buf, kTextX, 0, fg);
  row_push(row, sc);
}

// ---- images ------------------------------------------------------------------------------------------------

// One third `a`, two thirds `b` (RGB565 channels).
uint16_t blend565(uint16_t a, uint16_t b) {
  const unsigned r = (((a >> 11) & 0x1Fu) + 2u * ((b >> 11) & 0x1Fu)) / 3u;
  const unsigned g = (((a >> 5) & 0x3Fu) + 2u * ((b >> 5) & 0x3Fu)) / 3u;
  const unsigned bl = ((a & 0x1Fu) + 2u * (b & 0x1Fu)) / 3u;
  return static_cast<uint16_t>((r << 11) | (g << 5) | bl);
}

// Draws the rows of `img` that fall into the band; (x, y) is the image origin in band coordinates.
void blit(const assets::Image& img, int x, int y, bool dim, uint16_t bg) {
  if (img.px == nullptr) return;
  for (int r = 0; r < img.h; ++r) {
    const int yy = y + r;
    if (yy < 0) continue;
    if (yy >= kBandH) break;
    const uint16_t* row = img.px + static_cast<size_t>(r) * img.w;
    for (int c = 0; c < img.w; ++c) {
      uint16_t p = row[c];
      if (p == assets::kTransparent) continue;
      if (dim) p = blend565(p, bg);
      s_band.drawPixel(x + c, yy, p);
    }
  }
}

// ---- tiles -------------------------------------------------------------------------------------------------

// Tile at (x, y) in band coordinates: [stripe][icon 24][marker + label / status line].
void tile_cell(const Tile& t, int x, int y, int w, bool selected) {
  const uint16_t bg = selected ? kSurfaceFocus : kSurfacePanel;
  if (selected) {
    s_band.fillRect(x, y, w, kTileH, kSurfaceFocus);
    s_band.fillRect(x, y, kStripeW, kTileH, kAccent);
  }
  const int ix = x + kStripeW + 2;
  if (t.icon < assets::IconId::Count) {
    blit(assets::icon(t.icon), ix, y + 2, !t.enabled, bg);
  } else {  // no bitmap (Help): outlined '?' glyph of the same size
    s_band.drawRoundRect(ix + 1, y + 3, assets::kIconSize - 2, assets::kIconSize - 2, 4, kAccent);
    text("?", ix + (assets::kIconSize - kBodyW) / 2, y + 2 + (assets::kIconSize - kBodyH) / 2, kAccent);
  }
  const int tx = ix + assets::kIconSize + 3;
  const int cols = (x + w - 1 - tx) / kBodyW;
  if (cols >= 2) {
    char lab[kListCols + 1];
    fit(t.label ? t.label : "", SIZE_MAX, lab, cols - 1 < kListCols ? cols - 1 : kListCols);
    if (selected) text(">", tx, y + 2, kAccent);
    text(lab, tx + kBodyW, y + 2, t.enabled ? kTextPrimary : kTextMuted);
  }
  if (t.detail && t.detail[0]) {
    const int dcols = (x + w - 1 - tx) / kMetaW;
    char d[kSmallCols + 1];
    fit(t.detail, SIZE_MAX, d, dcols < kSmallCols ? dcols : kSmallCols);
    text(d, tx, y + 19, kTextMuted, true);
  }
}

// ---- footer ------------------------------------------------------------------------------------------------

// Draws `s` one character per cell; in a token "Key:Action" whose key part has 1..5 characters the key part
// uses `key_fg`, everything else `fg`.
void hint_text(const char* s, int x, int y, bool small, uint16_t fg, uint16_t key_fg) {
  const int cw = small ? kMetaW : kBodyW;
  bool in_key = false;
  for (size_t i = 0; s[i] != '\0'; ++i) {
    if (s[i] == ' ' || s[i] == ':') {
      in_key = false;
    } else if (i == 0 || s[i - 1] == ' ') {
      size_t j = i;
      while (s[j] != '\0' && s[j] != ' ' && s[j] != ':') ++j;
      in_key = s[j] == ':' && j - i <= 5;
    }
    const char ch[2] = {s[i], '\0'};
    text(ch, x + static_cast<int>(i) * cw, y, in_key ? key_fg : fg, small);
  }
}

}  // namespace

// ===== public API ===========================================================================================

uint16_t wrapped_line_count(const char* body) {
  uint16_t lines = 0;
  char out[kTextCols + 1];
  const char* p = body;
  size_t used;
  while (p != nullptr && (used = wrap_next(p, kTextCols, out)) > 0 && lines < 0xFFFF) {
    p += used;
    ++lines;
  }
  return lines;
}

const char* skip_wrapped_lines(const char* body, uint16_t n) {
  char out[kTextCols + 1];
  const char* p = body ? body : "";
  for (uint16_t i = 0; i < n; ++i) {
    const size_t used = wrap_next(p, kTextCols, out);
    if (used == 0) break;
    p += used;
  }
  return p;
}

void begin() {
  if (s_ready) return;
  s_ready = make(s_band, kScreenW, kBandH);
  if (!s_ready) s_band.deleteSprite();  // out of RAM: drawing becomes a no-op
}

bool ready() { return s_ready; }

void draw_status(ModeBanner mode, const char* session_short, const char* backend, int battery_pct, Conn conn) {
  if (!s_ready) return;
  const char* label = "OFFLINE";
  uint16_t fg = kTextMuted, bg = kSurfacePanel;
  switch (mode) {
    case ModeBanner::LiveRadio: label = "LIVE RADIO"; fg = kSurfaceBase; bg = kAccent; break;
    case ModeBanner::AiConnected: label = "AI CONNECTED"; fg = kAccent; bg = kSurfaceFocus; break;
    case ModeBanner::BackendOk: label = "BACKEND OK"; fg = kTextPrimary; bg = kSurfaceFocus; break;
    case ModeBanner::StartFailed: label = "START FAILED"; fg = kSurfaceBase; bg = kError; break;
    case ModeBanner::Simulated: label = "SIMULATED"; fg = kSurfaceBase; bg = kWarning; break;
    case ModeBanner::Offline:
    default: break;
  }
  band_fill(kSurfaceBase);
  s_band.drawFastHLine(0, kStatusH, kScreenW, kSurfacePanel);  // divider at y 15
  const int banner_w = static_cast<int>(std::strlen(label)) * kMetaW + 6;
  s_band.fillRoundRect(kPadX, 2, banner_w, 11, 2, bg);
  text(label, kPadX + 3, 4, fg, true);

  char batt[8];
  if (battery_pct >= 0 && battery_pct <= 100) {
    std::snprintf(batt, sizeof(batt), "%d%%", battery_pct);
  } else {
    std::snprintf(batt, sizeof(batt), "--%%");
  }
  const int batt_x = kPadX + kInnerW - static_cast<int>(std::strlen(batt)) * kMetaW;
  text(batt, batt_x, 4, kTextPrimary, true);

  char be[12];
  fit(backend ? backend : "", SIZE_MAX, be, 10);
  const int be_x = batt_x - 6 - static_cast<int>(std::strlen(be)) * kMetaW;
  text(be, be_x, 4, kTextPrimary, true);

  int left_end = be_x - 4;
  if (conn != Conn::Unknown) {
    const int cx = be_x - 5;
    switch (conn) {
      case Conn::Online: s_band.fillCircle(cx, 7, 2, kAccent); break;
      case Conn::Connecting: s_band.fillCircle(cx, 7, 2, kWarning); break;
      case Conn::Error: s_band.fillCircle(cx, 7, 2, kError); break;
      case Conn::Offline:
      case Conn::Unknown:
      default: s_band.drawCircle(cx, 7, 2, kTextMuted); break;
    }
    left_end = cx - 6;
  }

  const int sess_x = kPadX + banner_w + 4;
  const int sess_cols = (left_end - sess_x) / kMetaW;
  if (sess_cols > 1) {
    char sess[kSmallCols + 1];
    fit(session_short && session_short[0] ? session_short : "no-session", SIZE_MAX, sess,
        sess_cols < kSmallCols ? sess_cols : kSmallCols);
    text(sess, sess_x, 4, kTextMuted, true);
  }
  band_push(kStatusY);
}

void draw_list(const ListModel& m) {
  if (!s_ready) return;
  char pos[16];
  if (m.count > 0) {
    std::snprintf(pos, sizeof(pos), "%u/%u", static_cast<unsigned>(m.selected) + 1, static_cast<unsigned>(m.count));
  } else {
    std::snprintf(pos, sizeof(pos), "0/0");
  }
  heading(m.title, pos);
  content_pads(true);
  const Scroll sc{m.count, static_cast<uint32_t>(kRows), m.top};
  for (int r = 0; r < kRows; ++r) {
    const uint32_t i = static_cast<uint32_t>(m.top) + static_cast<uint32_t>(r);
    if (i < m.count) {
      char buf[64];
      buf[0] = '\0';
      if (m.row_text) m.row_text(static_cast<uint16_t>(i), buf, sizeof(buf));
      const bool disabled = m.row_disabled != nullptr && m.row_disabled(static_cast<uint16_t>(i));
      list_row(r, buf, i == m.selected, disabled ? kTextMuted : kTextPrimary, &sc);
    } else if (m.count == 0 && r == 0) {
      list_row(r, m.empty ? m.empty : "(empty)", false, kTextMuted, &sc);
    } else {
      list_row(r, "", false, kTextPrimary, &sc);
    }
  }
}

void draw_tiles(const TileModel& m) {
  if (!s_ready) return;
  heading(m.title, m.right);
  content_pads(false);
  const int cols = m.columns >= 2 ? 2 : 1;
  const int cell_w = kInnerW / cols;
  const uint32_t rows_total = (static_cast<uint32_t>(m.count) + cols - 1) / cols;
  const Scroll sc{rows_total, static_cast<uint32_t>(kTileRows), m.top_row};
  for (int vr = 0; vr < kTileRows; ++vr) {
    const int y0 = kContentY + vr * kTileH;
    // Two bands per tile row, y0..y0+15 and y0+12..y0+27; the shared rows are painted identically.
    for (int off = 0;; off = kTileH - kBandH) {
      band_fill(kSurfaceBase);
      band_panel(kSurfacePanel);
      for (int c = 0; c < cols; ++c) {
        const uint32_t idx = (static_cast<uint32_t>(m.top_row) + vr) * cols + c;
        if (idx < m.count) tile_cell(m.tiles[idx], kPadX + c * cell_w, -off, cell_w, idx == m.selected);
      }
      scrollbar(sc, kContentY, kContentH, y0 + off);
      band_push(y0 + off);
      if (off != 0) break;
    }
  }
}

void draw_footer(const char* left, const char* right) {
  if (!s_ready) return;
  const char* l = left ? left : "";
  const bool alert = l[0] == '!';
  if (alert) ++l;
  const uint16_t fg = alert ? kSurfaceBase : kTextPrimary;
  const uint16_t key_fg = alert ? kSurfaceBase : kAccent;
  const uint16_t right_fg = alert ? kSurfaceBase : kTextMuted;
  band_fill(alert ? kError : kSurfaceBase);

  char rbuf[kSmallCols + 1];
  fit(right ? right : "", SIZE_MAX, rbuf, 20);
  const int rn = static_cast<int>(std::strlen(rbuf));
  const int ln = static_cast<int>(std::strlen(l));
  if (ln + (rn > 0 ? rn + 1 : 0) <= kFooterCols) {
    if (rn > 0) hint_text(rbuf, kPadX + kInnerW - rn * kBodyW, 0, false, right_fg, key_fg);
    char lbuf[kFooterCols + 1];
    fit(l, SIZE_MAX, lbuf, kFooterCols);
    hint_text(lbuf, kPadX, 0, false, fg, key_fg);
  } else {
    // Too long for the 8x16 font: 6x8 font, one line, or two when the left text needs it.
    const int right_x = kPadX + kInnerW - rn * kMetaW;
    const int cols1 = (right_x - kPadX) / kMetaW - (rn > 0 ? 1 : 0);
    char lbuf[kSmallCols + 1];
    if (ln <= cols1) {
      if (rn > 0) hint_text(rbuf, right_x, 4, true, right_fg, key_fg);
      fit(l, SIZE_MAX, lbuf, cols1 > 0 ? cols1 : 1);
      hint_text(lbuf, kPadX, 4, true, fg, key_fg);
    } else {
      if (rn > 0) hint_text(rbuf, right_x, 0, true, right_fg, key_fg);
      const size_t used = wrap_next(l, cols1 > 0 ? cols1 : 1, lbuf);
      hint_text(lbuf, kPadX, 0, true, fg, key_fg);
      fit(l + used, SIZE_MAX, lbuf, kSmallCols);
      hint_text(lbuf, kPadX, 8, true, fg, key_fg);
    }
  }
  band_push(kFooterY);
}

void draw_notice(const char* msg) {
  if (!s_ready) return;
  band_fill(kSurfaceBase);
  band_panel(kSurfaceFocus);
  s_band.fillRect(kPadX, 0, kStripeW, kBandH, kAccent);
  const int x = kPadX + kStripeW + 3;
  const int w = kPadX + kInnerW - 2 - x;
  const char* t = msg ? msg : "";
  const size_t n = std::strlen(t);
  char buf[kSmallCols + 1];
  if (n <= static_cast<size_t>(w / kBodyW)) {
    fit(t, SIZE_MAX, buf, w / kBodyW);
    text(buf, x, 0, kTextPrimary);
  } else {
    const int cols = w / kMetaW;
    const size_t used = wrap_next(t, cols, buf);
    text(buf, x, 0, kTextPrimary, true);
    fit(t + used, SIZE_MAX, buf, cols);
    text(buf, x, 8, kTextPrimary, true);
  }
  band_push(kFooterY);
}

void draw_progress(const char* label, uint32_t elapsed_s, uint32_t items, bool cancellable) {
  if (!s_ready) return;
  char right[16];
  std::snprintf(right, sizeof(right), "%lus", static_cast<unsigned long>(elapsed_s));
  heading_tone("IN PROGRESS", right, Tone::Normal);
  content_pads(true);
  text_row(0, label ? label : "", kTextPrimary, nullptr);
  char line[40];
  std::snprintf(line, sizeof(line), "Elapsed: %lu s", static_cast<unsigned long>(elapsed_s));
  text_row(1, line, kTextMuted, nullptr);
  std::snprintf(line, sizeof(line), "Items so far: %lu", static_cast<unsigned long>(items));
  text_row(2, line, kTextMuted, nullptr);
  // Activity bar: moves one step per redraw (the caller redraws once a second), no timer of its own.
  row_begin(false);
  const int track_w = kTextCols * kBodyW;
  constexpr int kSeg = 40;
  constexpr uint32_t kSteps = 6;
  s_band.fillRect(kTextX, 6, track_w, 4, kSurfaceFocus);
  s_band.fillRect(kTextX + static_cast<int>(elapsed_s % kSteps) * (track_w - kSeg) / static_cast<int>(kSteps - 1), 6,
                  kSeg, 4, kAccent);
  row_push(3, nullptr);
  if (cancellable) {
    text_row(4, "Cancel: Fn+` or Ctrl+C", kWarning, nullptr);
  } else {
    text_row(4, "(cannot be cancelled)", kTextMuted, nullptr);
  }
}

void draw_confirm(const char* operation, const char* target, const char* effect) {
  if (!s_ready) return;
  heading_tone("CONFIRM ACTION", nullptr, Tone::Warn);
  content_pads(true);
  char line[64];
  std::snprintf(line, sizeof(line), "Op: %s", operation ? operation : "?");
  text_row(0, line, kTextPrimary, nullptr, true);
  std::snprintf(line, sizeof(line), "Target: %s", target && target[0] ? target : "(none)");
  text_row(1, line, kTextPrimary, nullptr, true);
  const char* p = effect ? effect : "";
  for (int r = 2; r < kRows; ++r) {
    char out[kTextCols + 2];
    const size_t used = wrap_next(p, kTextCols, out);
    p += used;
    if (r == kRows - 1 && *p != '\0') {
      const size_t n = std::strlen(out);  // effect longer than three rows
      out[n < static_cast<size_t>(kTextCols) ? n : kTextCols - 1] = '~';
      out[n < static_cast<size_t>(kTextCols) ? n + 1 : kTextCols] = '\0';
    }
    text_row(r, out, kTextPrimary, nullptr, true);
  }
}

void draw_message(const char* title, const char* body, uint16_t first_line, uint16_t total_lines) {
  if (!s_ready) return;
  heading(title, nullptr);
  content_pads(true);
  const Scroll sc{total_lines, static_cast<uint32_t>(kRows), first_line};
  const char* p = body ? body : "";
  for (int r = 0; r < kRows; ++r) {
    char out[kTextCols + 1];
    p += wrap_next(p, kTextCols, out);
    text_row(r, out, kTextPrimary, &sc);
  }
}

void draw_text_entry(const char* prompt, const char* buffer, size_t cursor, const char* hint) {
  if (!s_ready) return;
  heading(prompt, nullptr);
  content_pads(true);
  const char* b = buffer ? buffer : "";
  const size_t len = std::strlen(b);
  if (cursor > len) cursor = len;
  constexpr int kEntryRows = kRows - 1;  // rows 0..3, row 4: character count
  const size_t cursor_line = cursor / kTextCols;
  const size_t first = cursor_line >= static_cast<size_t>(kEntryRows) ? cursor_line - (kEntryRows - 1) : 0;
  for (int r = 0; r < kEntryRows; ++r) {
    const size_t start = (first + static_cast<size_t>(r)) * kTextCols;
    const size_t n = start < len ? (len - start < static_cast<size_t>(kTextCols) ? len - start : kTextCols) : 0;
    char out[kTextCols + 1];
    fit(b + (start < len ? start : len), n, out, kTextCols);
    row_begin(false);
    text(out, kTextX, 0, kTextPrimary);
    if (first + static_cast<size_t>(r) == cursor_line) {
      const int cx = kTextX + static_cast<int>(cursor % kTextCols) * kBodyW;
      const char under[2] = {cursor < len ? (b[cursor] >= 0x20 && b[cursor] <= 0x7E ? b[cursor] : '?') : ' ', 0};
      s_band.fillRect(cx, 0, kBodyW, kRowH, kAccent);
      text(under, cx, 0, kSurfaceBase);
    }
    row_push(r, nullptr);
  }
  char info[24];
  std::snprintf(info, sizeof(info), "%u chars", static_cast<unsigned>(len));
  text_row(kRows - 1, hint ? hint : info, kTextMuted, nullptr);
}

void draw_image(const assets::Image& img, int x, int y, uint16_t bg, bool dim) {
  if (!s_ready || img.px == nullptr || img.w == 0 || img.h == 0) return;
  auto& d = M5Cardputer.Display;
  d.setClipRect(x, y, img.w, img.h);  // the band is full width: only the image rectangle is pushed
  for (int off = 0; off < img.h; off += kBandH) {
    band_fill(bg);
    blit(img, x, -off, dim, bg);
    band_push(y + off);
  }
  d.clearClipRect();
}

void draw_icon(assets::IconId id, int x, int y, uint16_t bg, bool dim) { draw_image(assets::icon(id), x, y, bg, dim); }

void draw_colour_table(const char* title, const ColourRow* rows, uint8_t count, uint8_t selected, uint8_t top) {
  if (!s_ready) return;
  char pos[16];
  std::snprintf(pos, sizeof(pos), "%u/%u", static_cast<unsigned>(count ? selected + 1 : 0), static_cast<unsigned>(count));
  heading(title, pos);
  content_pads(true);
  const Scroll sc{count, static_cast<uint32_t>(kRows), top};
  const int dot_x = kListTextX + 6, text_x = kListTextX + 16;
  const int cols = (kPadX + kInnerW - text_x) / kMetaW;
  for (int r = 0; r < kRows; ++r) {
    const uint32_t i = static_cast<uint32_t>(top) + static_cast<uint32_t>(r);
    row_begin(i == selected);
    if (i < count) {
      const ColourRow& e = rows[i];
      if (e.blink) {
        s_band.drawCircle(dot_x, 8, 5, e.colour);
        s_band.fillCircle(dot_x, 8, 2, e.colour);
      } else {
        s_band.fillCircle(dot_x, 8, 5, e.colour);
      }
      char buf[kSmallCols + 1];
      fit(e.label ? e.label : "", SIZE_MAX, buf, cols < kSmallCols ? cols : kSmallCols);
      text(buf, text_x, 0, kTextPrimary, true);
      fit(e.comment ? e.comment : "", SIZE_MAX, buf, cols < kSmallCols ? cols : kSmallCols);
      text(buf, text_x, 8, kTextMuted, true);
    }
    row_push(r, &sc);
  }
}

void draw_animation_frame(const assets::Animation& anim, uint8_t frame) {
  if (anim.px == nullptr || anim.frames == 0 || anim.w == 0 || anim.h == 0) return;
  const size_t pixels = static_cast<size_t>(anim.w) * anim.h;
  const assets::Image img{anim.w, anim.h, anim.px + pixels * (frame % anim.frames)};
  draw_image(img, anim.x, anim.y, kSurfaceBase);
}

namespace {
uint8_t s_loader_frame = 0;
uint32_t s_loader_ms = 0;
}  // namespace

static char s_start_left[32] = {};
static int s_start_pct = -2;  // -2: no start footer drawn yet

// Battery icon, right-aligned at `right` in band coordinates: 20x10 body, 2x4 nub, fill in 16 steps.
static void battery_icon(int right, int pct) {
  const int body_w = 20, body_h = 10, x = right - body_w - 2, y = 3;
  s_band.drawRect(x, y, body_w, body_h, kTextPrimary);
  s_band.fillRect(x + body_w, y + 3, 2, 4, kTextPrimary);
  if (pct < 0) return;  // unknown: empty outline
  int bars = (pct * 16 + 50) / 100;
  if (bars < 1 && pct > 0) bars = 1;
  const uint16_t c = pct >= 40 ? kAccent : (pct >= 20 ? kWarning : kError);
  if (bars > 0) s_band.fillRect(x + 2, y + 2, bars, body_h - 4, c);
}

void draw_start_screen(const char* left, const char* right) {
  draw_start_image();
  draw_footer(left, right);
  s_start_left[0] = '\0';
  s_start_pct = -2;
}

void draw_start_image() {
  draw_image(assets::splash(), 0, 0, 0);
  s_loader_frame = 0;
  s_loader_ms = millis();
}

void draw_start_footer(const char* left, int battery_pct) {
  if (!s_ready) return;
  std::snprintf(s_start_left, sizeof(s_start_left), "%s", left ? left : "");
  s_start_pct = battery_pct < 0 ? -1 : (battery_pct > 100 ? 100 : battery_pct);
  band_fill(kSurfaceBase);
  const int right = kPadX + kInnerW;
  char pct[8];
  if (s_start_pct >= 0) {
    std::snprintf(pct, sizeof(pct), "%d%%", s_start_pct);
  } else {
    std::snprintf(pct, sizeof(pct), "--%%");
  }
  const int icon_right = right;
  const int text_x = icon_right - 22 - 3 - static_cast<int>(std::strlen(pct)) * kMetaW;
  battery_icon(icon_right, s_start_pct);
  text(pct, text_x, 4, kTextMuted, true);
  const int cols = (text_x - 4 - kPadX) / kBodyW;
  char lbuf[kFooterCols + 1];
  fit(s_start_left, SIZE_MAX, lbuf, cols > 0 ? (cols < kFooterCols ? cols : kFooterCols) : 1);
  hint_text(lbuf, kPadX, 0, false, kTextPrimary, kAccent);
  band_push(kFooterY);
}

void refresh_start_footer(int battery_pct) {
  if (s_start_left[0] == '\0') return;  // nothing drawn by draw_start_footer
  const int p = battery_pct < 0 ? -1 : (battery_pct > 100 ? 100 : battery_pct);
  if (p != s_start_pct) draw_start_footer(s_start_left, p);
}

void tick_start_screen() {
  const assets::Animation& a = assets::loader();
  const uint32_t period = a.fps > 0 ? 1000u / a.fps : 200u;
  const uint32_t now = millis();
  if (now - s_loader_ms < period) return;
  s_loader_ms = now;
  draw_animation_frame(a, s_loader_frame);
  s_loader_frame = static_cast<uint8_t>((s_loader_frame + 1) % (a.frames > 0 ? a.frames : 1));
}

void clear_screen() { M5Cardputer.Display.fillScreen(kSurfaceBase); }

}  // namespace oyster::ui
