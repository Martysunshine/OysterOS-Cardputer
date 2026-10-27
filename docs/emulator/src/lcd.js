// OysterOS LCD: a JavaScript port of firmware/src/ui/widgets.cpp (and the LGFX shape routines it uses),
// drawing into a 240x135 RGB565-quantised framebuffer with the firmware's own font and icon data.
const LCD = (() => {
  const W = 240, H = 135;
  const PAD = 5, INNER = 230, STATUS_H = 15, HEAD_Y = 16, CONTENT_Y = 33, CONTENT_H = 84, FOOT_Y = 119;
  const BAND = 16, STRIPE = 3, ROW_H = 16, ROWS = 5, ROWS_Y = 34, TILE_H = 28, TILE_ROWS = 3;
  const SCROLL_X = 236, SCROLL_W = 3, BODY_W = 8, META_W = 6;
  const LIST_X = PAD + STRIPE + BODY_W, LIST_COLS = 27, TEXT_X = PAD + 4, TEXT_COLS = 28, FOOT_COLS = 28, SMALL_COLS = 38;
  const q = rgb => { // 0xRRGGBB -> the colour the RGB565 panel actually shows
    const r = (rgb >> 19) & 31, g = (rgb >> 10) & 63, b = (rgb >> 3) & 31;
    return [(r * 255 / 31) | 0, (g * 255 / 63) | 0, (b * 255 / 31) | 0];
  };
  const C = { base: q(0x101923), panel: q(0x192C3D), focus: q(0x234B54), accent: q(0x5EEAD4),
    text: q(0xF4FAFF), muted: q(0xA9C4CE), warn: q(0xE9AD52), err: q(0xF17A7A) };
  const TRANSPARENT = 0xF81F;
  let A = null, f16 = null, f8 = null, icons = {}, splash = null, loader = null;
  const fb = new Uint8ClampedArray(W * H * 4);
  // The band: every region is painted into a 240x16 strip and pushed, exactly like the firmware.
  const band = new Uint8ClampedArray(W * BAND * 4);
  let clipY0 = 0, clipY1 = BAND;

  const u16 = b64 => { const s = atob(b64), a = new Uint16Array(s.length / 2);
    for (let i = 0; i < a.length; i++) a[i] = s.charCodeAt(2 * i) | (s.charCodeAt(2 * i + 1) << 8); return a; };
  const u8 = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const c565 = p => [(((p >> 11) & 31) * 255 / 31) | 0, (((p >> 5) & 63) * 255 / 63) | 0, ((p & 31) * 255 / 31) | 0];

  function init(assets) {
    A = assets; f16 = u8(A.f16); f8 = u8(A.f8);
    for (const [k, v] of Object.entries(A.icons)) icons[k] = u16(v);
    splash = u16(A.splash); loader = u16(A.loader);
  }

  // ---- band primitives ----
  function px(x, y, c) {
    if (x < 0 || x >= W || y < clipY0 || y >= clipY1 || y < 0 || y >= BAND) return;
    const o = (y * W + x) * 4; band[o] = c[0]; band[o + 1] = c[1]; band[o + 2] = c[2]; band[o + 3] = 255;
  }
  function rect(x, y, w, h, c) { for (let j = y; j < y + h; j++) for (let i = x; i < x + w; i++) px(i, j, c); }
  const hline = (x, y, w, c) => rect(x, y, w, 1, c), vline = (x, y, h, c) => rect(x, y, 1, h, c);
  function fill(c) { for (let i = 0; i < W * BAND; i++) { band[i * 4] = c[0]; band[i * 4 + 1] = c[1]; band[i * 4 + 2] = c[2]; band[i * 4 + 3] = 255; } }
  function push(y) {
    for (let j = 0; j < BAND; j++) { const yy = y + j; if (yy < 0 || yy >= H) continue;
      fb.set(band.subarray(j * W * 4, (j + 1) * W * 4), yy * W * 4); }
  }
  const panel = c => rect(PAD, 0, INNER, BAND, c);
  // Direct-to-display drawing (draw_calc writes past the band).
  function dpx(x, y, c) { if (x < 0 || x >= W || y < 0 || y >= H) return; const o = (y * W + x) * 4; fb[o] = c[0]; fb[o + 1] = c[1]; fb[o + 2] = c[2]; fb[o + 3] = 255; }
  function drect(x, y, w, h, c) { for (let j = y; j < y + h; j++) for (let i = x; i < x + w; i++) dpx(i, j, c); }

  // LGFX shapes, parameterised by a rect function so they work on the band or the display.
  function fillCircle(R, x, y, r, c) {
    R(x - r, y, (r << 1) + 1, 1, c); if (r <= 0) return;
    let delta = 1, f = 1 - r, ddy = -(r << 1), ddx = 1, i = 0;
    do { let ln = 0; while (f < 0) { ddx += 2; f += ddx; ln++; } i += ln; ddy += 2; f += ddy;
      if (ln) R(x - r, y + i - ln + 1, (r << 1) + delta, ln, c);
      R(x - i, y + r, (i << 1) + delta, 1, c); R(x - i, y - r, (i << 1) + delta, 1, c);
      if (ln) R(x - r, y - i, (r << 1) + delta, ln, c); r--; } while (i < r);
  }
  function drawCircle(R, x, y, r, c) {
    if (r <= 0) { R(x, y, 1, 1, c); return; }
    let f = 1 - r, ddy = -(r << 1), ddx = 1, i = 0, j = -1;
    do { while (f < 0) { i++; ddx += 2; f += ddx; } ddy += 2; f += ddy; const n = i - j;
      R(x - i, y + r, n, 1, c); R(x - i, y - r, n, 1, c); R(x + j + 1, y - r, n, 1, c); R(x + j + 1, y + r, n, 1, c);
      R(x + r, y + j + 1, 1, n, c); R(x + r, y - i, 1, n, c); R(x - r, y - i, 1, n, c); R(x - r, y + j + 1, 1, n, c);
      j = i; r--; } while (i < r);
  }
  function drawRoundRect(R, x, y, w, h, r, c) {
    w--; h--; let len = (r << 1) + 1; const y1 = y + h - r, y0 = y + r;
    R(x, y0 + 1, 1, h - len, c); R(x + w, y0 + 1, 1, h - len, c);
    const x1 = x + w - r, x0 = x + r; R(x0 + 1, y, w - len, 1, c); R(x0 + 1, y + h, w - len, 1, c);
    let f = 1 - r, ddy = -(r << 1), ddx = 1; len = 0;
    for (let i = 0; i <= r; i++) { len++;
      if (f >= 0) { R(x0 - i, y0 - r, len, 1, c); R(x0 - i, y1 + r, len, 1, c); R(x1 + i - len + 1, y1 + r, len, 1, c); R(x1 + i - len + 1, y0 - r, len, 1, c);
        R(x1 + r, y1 + i - len + 1, 1, len, c); R(x0 - r, y1 + i - len + 1, 1, len, c); R(x1 + r, y0 - i, 1, len, c); R(x0 - r, y0 - i, 1, len, c);
        len = 0; r--; ddy += 2; f += ddy; }
      ddx += 2; f += ddx; }
  }
  function fillRoundRect(R, x, y, w, h, r, c) {
    const y2 = y + r, y1 = y + h - r - 1; let ddy = -(r << 1); const delta = w + ddy;
    R(x, y2, w, h + ddy, c); const x0 = x + r; let f = 1 - r, ddx = 1, len = 0;
    for (let i = 0; i <= r; i++) { len++;
      if (f >= 0) { R(x0 - r, y2 - i, (r << 1) + delta, len, c); R(x0 - r, y1 + i - len + 1, (r << 1) + delta, len, c);
        if (i === r) break; len = 0; R(x0 - i, y1 + r, (i << 1) + delta, 1, c); ddy += 2; f += ddy; R(x0 - i, y2 - r, (i << 1) + delta, 1, c); r--; }
      ddx += 2; f += ddx; }
  }

  // ---- text (top-left datum, transparent background) ----
  function glyph(P, ch, x, y, c, small, scale = 1) {
    const code = ch.charCodeAt(0); const idx = (code >= 0x20 && code <= 0x7E ? code : 0x3F) - 0x20;
    if (small) { for (let col = 0; col < 5; col++) { const bits = f8[idx * 5 + col];
        for (let row = 0; row < 8; row++) if (bits & (1 << row)) P(x + col, y + row, c); } }
    else { for (let row = 0; row < 16; row++) { const bits = f16[idx * 16 + row];
        for (let col = 0; col < 8; col++) if (bits & (0x80 >> col))
          for (let sy = 0; sy < scale; sy++) for (let sx = 0; sx < scale; sx++) P(x + col * scale + sx, y + row * scale + sy, c); } }
  }
  function text(s, x, y, c, small = false) {
    const w = small ? META_W : BODY_W;
    for (const ch of s) { if (ch !== " ") glyph(px, ch, x, y, c, small); x += w; }
  }
  // fit(): printable copy of at most cols characters, '~' marks truncation.
  function fit(s, cols) {
    s = (s || "").replace(/[^\x20-\x7E]/g, "?");
    return s.length > cols ? s.slice(0, cols - 1) + "~" : s;
  }
  // wrap_next(): next word-wrapped line; returns [line, consumed].
  function wrapNext(s, cols) {
    if (!s) return ["", 0];
    let n = 0, last = -1;
    while (n < s.length && s[n] !== "\n" && n < cols) { if (s[n] === " ") last = n; n++; }
    let take = n, used = take;
    if (s[n] === "\n") used = take + 1;
    else if (n < s.length && s[n] !== " " && last > 0) { take = last; used = take + 1; }
    else if (s[n] === " ") used = take + 1;
    return [fit(s.slice(0, take), cols), used];
  }
  function wrappedLines(body) { const out = []; let p = body || "";
    while (p.length) { const [l, u] = wrapNext(p, TEXT_COLS); if (!u) break; out.push(l); p = p.slice(u); } return out; }

  function scrollbar(total, visible, first, regionY, regionH, bandY) {
    if (!visible || total <= visible) return;
    const y = regionY - bandY; rect(SCROLL_X, y, SCROLL_W, regionH, C.panel);
    let th = Math.floor(regionH * visible / total); if (th < 6) th = 6;
    const span = total - visible, f = Math.min(first, span);
    rect(SCROLL_X, y + Math.floor((regionH - th) * f / span), SCROLL_W, th, C.accent);
  }
  function padRows(y, h) { if (h <= 0) return; drect(0, y, PAD, h, C.base); drect(PAD, y, INNER, h, C.panel); drect(PAD + INNER, y, W - PAD - INNER, h, C.base); }
  function contentPads(rowLayout) {
    drect(0, 32, W, CONTENT_Y - 32, C.base);
    if (rowLayout) { padRows(CONTENT_Y, ROWS_Y - CONTENT_Y); const below = ROWS_Y + ROWS * ROW_H; padRows(below, CONTENT_Y + CONTENT_H - below); }
    drect(0, CONTENT_Y + CONTENT_H, W, FOOT_Y - CONTENT_Y - CONTENT_H, C.base);
  }
  function heading(title, right, tone) {
    title = title || "";
    if (!tone && title[0] === "!") { tone = "alert"; title = title.slice(1); }
    const bg = tone === "alert" ? C.err : tone === "warn" ? C.warn : C.base;
    const fg = tone ? C.base : C.text;
    fill(bg); let rx = PAD + INNER;
    if (right) { const r = fit(right, 12); rx -= r.length * BODY_W; text(r, rx, 0, tone ? fg : C.muted); }
    const cols = Math.floor((rx - PAD - 1 - BODY_W) / BODY_W);
    const t = fit(title, cols > 0 ? cols : 1); text(t, PAD, 0, fg); text(t, PAD + 1, 0, fg);
    push(HEAD_Y);
  }
  function rowBegin(sel, warnStripe) {
    fill(C.base); panel(sel ? C.focus : C.panel);
    if (sel) { rect(PAD, 0, STRIPE, ROW_H, C.accent); text(">", PAD + STRIPE, 0, C.accent); }
    else if (warnStripe) rect(PAD, 0, STRIPE, ROW_H, C.warn);
  }
  function rowPush(row, sc) { const y = ROWS_Y + row * ROW_H; if (sc) scrollbar(sc[0], sc[1], sc[2], ROWS_Y, ROWS * ROW_H, y); push(y); }
  function textRow(row, s, fg, sc, warn) { rowBegin(false, warn); text(fit(s, TEXT_COLS), TEXT_X, 0, fg); rowPush(row, sc); }

  function blend(p, bg) { const a = c565(p); return [((a[0] + 2 * bg[0]) / 3) | 0, ((a[1] + 2 * bg[1]) / 3) | 0, ((a[2] + 2 * bg[2]) / 3) | 0]; }
  function blit(img, w, h, x, y, dim, bg) {
    for (let r = 0; r < h; r++) for (let c = 0; c < w; c++) { const p = img[r * w + c]; if (p === TRANSPARENT) continue;
      px(x + c, y + r, dim ? blend(p, bg) : c565(p)); }
  }
  function tileCell(t, x, y, w, sel) {
    const bg = sel ? C.focus : C.panel;
    if (sel) { rect(x, y, w, TILE_H, C.focus); rect(x, y, STRIPE, TILE_H, C.accent); }
    const ix = x + STRIPE + 2;
    if (t.icon && icons[t.icon]) blit(icons[t.icon], 24, 24, ix, y + 2, !t.enabled, bg);
    else { drawRoundRect(rect, ix + 1, y + 3, 22, 22, 4, C.accent); text("?", ix + 8, y + 2 + 4, C.accent); }
    const tx = ix + 27, cols = Math.floor((x + w - 1 - tx) / BODY_W);
    if (cols >= 2) { if (sel) text(">", tx, y + 2, C.accent); text(fit(t.label, Math.min(cols - 1, LIST_COLS)), tx + BODY_W, y + 2, t.enabled ? C.text : C.muted); }
    if (t.detail) { const dc = Math.floor((x + w - 1 - tx) / META_W); text(fit(t.detail, Math.min(dc, SMALL_COLS)), tx, y + 19, C.muted, true); }
  }
  function hintText(s, x, y, small, fg, keyFg) {
    const cw = small ? META_W : BODY_W; let inKey = false;
    for (let i = 0; i < s.length; i++) {
      if (s[i] === " " || s[i] === ":") inKey = false;
      else if (i === 0 || s[i - 1] === " ") { let j = i; while (j < s.length && s[j] !== " " && s[j] !== ":") j++; inKey = s[j] === ":" && j - i <= 5; }
      if (s[i] !== " ") glyph(px, s[i], x + i * cw, y, inKey ? keyFg : fg, small);
    }
  }

  // ===== public drawing API (ui.h) =====
  function status(o) {
    const B = { LIVE: ["LIVE RADIO", C.base, C.accent], AI: ["AI CONNECTED", C.accent, C.focus], BACKEND: ["BACKEND OK", C.text, C.focus],
      ONLINE: ["ONLINE", C.base, C.accent], OFFLINE: ["OFFLINE", C.muted, C.panel] }[o.mode] || ["OFFLINE", C.muted, C.panel];
    fill(C.base); rect(0, STATUS_H, W, 1, C.panel);
    const bw = B[0].length * META_W + 6; fillRoundRect(rect, PAD, 2, bw, 11, 2, B[2]); text(B[0], PAD + 3, 4, B[1], true);
    const batt = o.battery + "%", bx = PAD + INNER - batt.length * META_W; text(batt, bx, 4, C.text, true);
    const be = fit(o.backend, 10), bex = bx - 6 - be.length * META_W; text(be, bex, 4, C.text, true);
    let left = bex - 4;
    const dot = (cx, s) => s === "on" ? fillCircle(rect, cx, 7, 2, C.accent) : s === "wait" ? fillCircle(rect, cx, 7, 2, C.warn) : s === "err" ? fillCircle(rect, cx, 7, 2, C.err) : drawCircle(rect, cx, 7, 2, C.muted);
    if (o.conn) { const cx = bex - 5; dot(cx, o.conn); left = cx - 6; }
    if (o.wifi) { const wtx = left - 4 * META_W; text("wifi", wtx, 4, C.text, true); const wcx = wtx - 5; dot(wcx, o.wifi); left = wcx - 6; }
    const sx = PAD + bw + 4, sc = Math.floor((left - sx) / META_W);
    if (sc > 1) text(fit(o.session || "no-session", Math.min(sc, SMALL_COLS)), sx, 4, C.muted, true);
    push(0);
  }
  function list(m) {
    heading(m.title, m.count ? `${m.sel + 1}/${m.count}` : "0/0"); contentPads(true);
    const sc = [m.count, ROWS, m.top];
    for (let r = 0; r < ROWS; r++) { const i = m.top + r;
      rowBegin(i === m.sel && i < m.count);
      if (i < m.count) text(fit(m.rows[i], LIST_COLS), LIST_X, 0, m.disabled && m.disabled[i] ? C.muted : C.text);
      else if (m.count === 0 && r === 0) text(fit(m.empty || "(empty)", LIST_COLS), LIST_X, 0, C.muted);
      rowPush(r, sc); }
  }
  function tiles(m) {
    heading(m.title, m.right); contentPads(false);
    const cols = m.cols || 2, cw = INNER / cols, total = Math.ceil(m.tiles.length / cols);
    for (let vr = 0; vr < TILE_ROWS; vr++) { const y0 = CONTENT_Y + vr * TILE_H;
      for (const off of [0, TILE_H - BAND]) { fill(C.base); panel(C.panel);
        for (let c = 0; c < cols; c++) { const idx = (m.top + vr) * cols + c; if (idx < m.tiles.length) tileCell(m.tiles[idx], PAD + c * cw, -off, cw, idx === m.sel); }
        scrollbar(total, TILE_ROWS, m.top, CONTENT_Y, CONTENT_H, y0 + off); push(y0 + off); } }
  }
  function colourTable(title, rows, sel, top) {
    heading(title, `${rows.length ? sel + 1 : 0}/${rows.length}`); contentPads(true);
    const dotX = LIST_X + 6, tx = LIST_X + 16, cols = Math.min(Math.floor((PAD + INNER - tx) / META_W), SMALL_COLS);
    for (let r = 0; r < ROWS; r++) { const i = top + r; rowBegin(i === sel);
      if (i < rows.length) { const e = rows[i], c = q(e[2]);
        if (e[3]) { drawCircle(rect, dotX, 8, 5, c); fillCircle(rect, dotX, 8, 2, c); } else fillCircle(rect, dotX, 8, 5, c);
        text(fit(e[0], cols), tx, 0, C.text, true); text(fit(e[1], cols), tx, 8, C.muted, true); }
      rowPush(r, [rows.length, ROWS, top]); }
  }
  function footer(left, right) {
    let l = left || ""; const alert = l[0] === "!"; if (alert) l = l.slice(1);
    const fg = alert ? C.base : C.text, kf = alert ? C.base : C.accent, rf = alert ? C.base : C.muted;
    fill(alert ? C.err : C.base); const r = fit(right || "", 20), rn = r.length, ln = l.length;
    if (ln + (rn ? rn + 1 : 0) <= FOOT_COLS) { if (rn) hintText(r, PAD + INNER - rn * BODY_W, 0, false, rf, kf); hintText(fit(l, FOOT_COLS), PAD, 0, false, fg, kf); }
    else { const rx = PAD + INNER - rn * META_W, c1 = Math.floor((rx - PAD) / META_W) - (rn ? 1 : 0);
      if (ln <= c1) { if (rn) hintText(r, rx, 4, true, rf, kf); hintText(fit(l, Math.max(c1, 1)), PAD, 4, true, fg, kf); }
      else { if (rn) hintText(r, rx, 0, true, rf, kf); const [a, u] = wrapNext(l, Math.max(c1, 1)); hintText(a, PAD, 0, true, fg, kf); hintText(fit(l.slice(u), SMALL_COLS), PAD, 8, true, fg, kf); } }
    push(FOOT_Y);
  }
  function notice(msg) {
    fill(C.base); panel(C.focus); rect(PAD, 0, STRIPE, BAND, C.accent);
    const x = PAD + STRIPE + 3, w = PAD + INNER - 2 - x;
    if (msg.length <= Math.floor(w / BODY_W)) text(fit(msg, Math.floor(w / BODY_W)), x, 0, C.text);
    else { const cols = Math.floor(w / META_W), [a, u] = wrapNext(msg, cols); text(a, x, 0, C.text, true); text(fit(msg.slice(u), cols), x, 8, C.text, true); }
    push(FOOT_Y);
  }
  function message(title, body, first = 0, total = 0) {
    heading(title); contentPads(true); const lines = wrappedLines(body);
    for (let r = 0; r < ROWS; r++) textRow(r, lines[first + r] || "", C.text, [total, ROWS, first]);
  }
  function progress(label, secs, items) {
    heading("IN PROGRESS", secs + "s"); contentPads(true);
    textRow(0, label, C.text); textRow(1, `Elapsed: ${secs} s`, C.muted); textRow(2, `Items so far: ${items}`, C.muted);
    rowBegin(false); const tw = TEXT_COLS * BODY_W; rect(TEXT_X, 6, tw, 4, C.focus); rect(TEXT_X + (secs % 6) * (tw - 40) / 5, 6, 40, 4, C.accent); rowPush(3);
    textRow(4, "Cancel: Fn+` or Ctrl+C", C.warn);
  }
  function confirm(op, target, effect) {
    heading("CONFIRM ACTION", null, "warn"); contentPads(true);
    textRow(0, "Op: " + op, C.text, null, true); textRow(1, "Target: " + (target || "(none)"), C.text, null, true);
    let p = effect || ""; for (let r = 2; r < ROWS; r++) { const [l, u] = wrapNext(p, TEXT_COLS); p = p.slice(u); textRow(r, l, C.text, null, true); }
  }
  function entry(prompt, buf, cursor, hint) {
    heading(prompt); contentPads(true); const len = buf.length; cursor = Math.min(cursor, len);
    const cl = Math.floor(cursor / TEXT_COLS), first = cl >= 4 ? cl - 3 : 0;
    for (let r = 0; r < 4; r++) { const start = (first + r) * TEXT_COLS; rowBegin(false);
      text(fit(buf.slice(start, start + TEXT_COLS), TEXT_COLS), TEXT_X, 0, C.text);
      if (first + r === cl) { const cx = TEXT_X + (cursor % TEXT_COLS) * BODY_W; rect(cx, 0, BODY_W, ROW_H, C.accent); text(cursor < len ? buf[cursor] : " ", cx, 0, C.base); }
      rowPush(r); }
    textRow(4, hint || `${len} chars`, C.muted);
  }
  function calc(m) {
    heading(m.title); drect(0, 32, W, 1, C.base); padRows(CONTENT_Y, CONTENT_H); drect(0, CONTENT_Y + CONTENT_H, W, 2, C.base);
    const dx = PAD + 4, dw = INNER - 8, dy = CONTENT_Y + 2, dh = 50;
    fillRoundRect(drect, dx, dy, dw, dh, 4, C.base); drawRoundRect(drect, dx, dy, dw, dh, 4, C.accent);
    const right = dx + dw - 8, cols = Math.floor((dw - 16) / BODY_W);
    const rtext = (s, xr, y, c, scale = 1) => { let x = xr - s.length * BODY_W * scale; for (const ch of s) { if (ch !== " ") glyph(dpx, ch, x, y, c, false, scale); x += BODY_W * scale; } };
    rtext(fit(m.expr, cols), right, dy + 3, C.muted);
    const res = m.result || "0", rc = m.ok ? C.accent : C.err;
    if (res.length * 2 <= cols) rtext(res, right, dy + 17, rc, 2); else rtext(fit(res, cols), right, dy + 24, rc);
    const line = (s, x, y) => { for (const ch of s) { if (ch !== " ") glyph(dpx, ch, x, y, "+-*/%".includes(ch) ? C.warn : "()".includes(ch) ? C.accent : C.muted, false); x += BODY_W; } };
    const lx = PAD + 6, ly = dy + dh + 1; line("+  -  *  /  %  ( )", lx, ly); line(fit(m.prev || "", TEXT_COLS), lx, ly + 16);
  }
  function lockScreen(frame, battery) {
    for (let i = 0; i < W * H; i++) { const c = c565(splash[i]); fb[i * 4] = c[0]; fb[i * 4 + 1] = c[1]; fb[i * 4 + 2] = c[2]; fb[i * 4 + 3] = 255; }
    const fw = 80, base = frame * fw * fw;
    for (let r = 0; r < fw; r++) for (let c = 0; c < fw; c++) dpx(18 + c, 8 + r, c565(loader[base + r * fw + c]));
    // start footer: "Any key:Unlock" + battery icon
    fill(C.base); const right = PAD + INNER, pct = battery + "%", tx = right - 22 - 3 - pct.length * META_W;
    const x = right - 22, y = 3; rect(x, y, 20, 1, C.text); rect(x, y + 9, 20, 1, C.text); rect(x, y, 1, 10, C.text); rect(x + 19, y, 1, 10, C.text);
    rect(x + 20, y + 3, 2, 4, C.text); rect(x + 2, y + 2, Math.max(1, Math.round(battery * 16 / 100)), 6, battery >= 40 ? C.accent : battery >= 20 ? C.warn : C.err);
    text(pct, tx, 4, C.muted, true); hintText("Any key:Unlock", PAD, 0, false, C.text, C.accent); push(FOOT_Y);
  }
  return { init, fb, W, H, status, list, tiles, colourTable, footer, notice, message, progress, confirm, entry, calc, lockScreen, wrappedLines, ROWS };
})();
