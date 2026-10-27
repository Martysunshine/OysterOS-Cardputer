(() => {
  // Cardputer ADV keyboard, 4 x 14. [main, secondary] legends.
  const ROWS = [
    [["`","~"],["1","!"],["2","@"],["3","#"],["4","$"],["5","%"],["6","^"],["7","&"],["8","*"],["9","("],["0",")"],["-","_"],["=","+"],["del",""]],
    [["tab",""],["q"],["w"],["e"],["r"],["t"],["y"],["u"],["i"],["o"],["p"],["[","{"],["]","}"],["\\","|"]],
    [["fn",""],["shift",""],["a"],["s"],["d"],["f"],["g"],["h"],["j"],["k"],["l"],[";","↑"],["'","\""],["enter",""]],
    [["ctrl",""],["opt",""],["alt",""],["z"],["x"],["c"],["v"],["b"],["n"],["m"],[",","←"],[".","↓"],["/","→"],["space",""]],
  ];
  // Shifted characters of the keys whose second legend is a character (arrows are Fn-layer hints).
  const SHIFTED = { "`": "~", "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|", ";": ":", "'": "\"", ",": "<", ".": ">", "/": "?" };
  const LIVE = new Set(["`","1","2","3","4","5","6","7","8","9","del","fn","shift",";",",",".","/","enter"]);
  const kbd = document.getElementById("kbd"), keyEls = new Map();
  for (const row of ROWS) {
    const r = document.createElement("div"); r.className = "row";
    for (const [m, s] of row) {
      const b = document.createElement("button");
      b.className = "key" + (s ? " has-s" : "") + (m.length > 1 ? " word" : "") + (LIVE.has(m) ? " live" : "");
      b.dataset.k = m; b.setAttribute("aria-label", m === "`" ? "backtick" : m);
      b.innerHTML = (s ? `<span class="s">${s.replace("<", "&lt;").replace(">", "&gt;")}</span>` : "") + `<span class="m">${m === "space" ? "␣" : m}</span>`;
      r.appendChild(b); keyEls.set(m, b);
    }
    kbd.appendChild(r);
  }
  LCD.init(ASSETS);
  const canvas = document.getElementById("lcd"), ctx = canvas.getContext("2d");
  const img = ctx.createImageData(240, 135);
  const where = document.getElementById("where"), led = document.getElementById("led");
  const qs = new URLSearchParams(location.search), HOLD = qs.has("hold");
  if (qs.has("bare")) document.documentElement.classList.add("bare");
  let fn = false, shift = false, tourTimer = 0;
  function paint() {
    OS.draw(); img.data.set(LCD.fb); ctx.putImageData(img, 0, 0);
    canvas.style.filter = OS.state.brightness >= 5 ? "" : `brightness(${0.4 + OS.state.brightness * 0.12})`;
    const lc = "led " + (OS.state.locked ? "white" : "amber");
    if (led.className !== lc) led.className = lc;
    where.innerHTML = `<b>${OS.state.locked ? "LOCK" : "OYSTER OS"}</b> · ${OS.where()}`;
  }
  setInterval(paint, 80);
  setInterval(() => OS.tick(), 167);  // the loader animation runs at 6 fps, as on the device
  function flash(m) {
    const el = m === "G0" ? document.getElementById("g0") : keyEls.get(m); if (!el) return;
    el.classList.add("down"); if (!HOLD) setTimeout(() => el.classList.remove("down"), 130);
  }
  function unlatch() { fn = shift = false; keyEls.get("fn").classList.remove("latched"); keyEls.get("shift").classList.remove("latched"); }
  const NAV = { ";": "up", ".": "down", ",": "left", "/": "right" };
  // One press of a Cardputer key, with the Fn and Shift latches, turned into what the firmware would do.
  function devKey(m) {
    flash(m);
    if (m === "fn") { fn = !fn; keyEls.get("fn").classList.toggle("latched", fn); return; }
    if (m === "shift") { shift = !shift; keyEls.get("shift").classList.toggle("latched", shift); return; }
    const entry = OS.isEntry(), f = fn, s = shift; unlatch();
    if (m === "del") return OS.key(f ? "home" : "del");
    if (m === "enter") return OS.key("enter");
    if (["tab", "ctrl", "opt", "alt"].includes(m)) return;
    if (f && NAV[m]) return OS.key(NAV[m]);
    let ch = m === "space" ? " " : m;
    if (s) ch = SHIFTED[m] || ch.toUpperCase();
    if (ch === "?" && !entry) return OS.key("help");
    if (!entry && !s) { if (NAV[ch]) return OS.key(NAV[ch]); if (ch === "`") return OS.key("back"); }
    OS.key("char:" + ch);
  }
  kbd.addEventListener("click", e => { const b = e.target.closest(".key"); if (b) { stopTour(); devKey(b.dataset.k); paint(); } });
  document.getElementById("g0").addEventListener("click", () => { stopTour(); flash("G0"); OS.lock(); paint(); });
  document.getElementById("reset").addEventListener("click", () => { stopTour(); OS.state.locked = false; OS.key("home"); paint(); });
  const ARROWS = { ArrowUp: ";", ArrowDown: ".", ArrowLeft: ",", ArrowRight: "/" };
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest && e.target.closest("button") && (e.key === "Enter" || e.key === " ")) return;
    stopTour();
    const entry = OS.isEntry(); let handled = true;
    if (e.key === "Home") { flash("fn"); flash("del"); OS.key("home"); }
    else if (e.key === "F1") { flash("shift"); flash("/"); OS.key("help"); }
    else if (ARROWS[e.key]) { if (!entry) devKey(ARROWS[e.key]); }
    else if (e.key === "Enter") devKey("enter");
    else if (e.key === "Backspace") devKey("del");
    else if (e.key === "Escape") { flash("`"); OS.key(entry ? "del" : "back"); }
    else if (e.key === " ") { flash("space"); OS.key("char: "); }
    else if (e.key.length === 1 && e.key >= " " && e.key <= "~") {
      const lower = e.key.toLowerCase();
      const base = keyEls.has(lower) ? lower : Object.keys(SHIFTED).find(k => SHIFTED[k] === e.key);
      if (base) flash(base);
      if (e.key !== lower || (base && SHIFTED[base] === e.key)) flash("shift");
      if (e.key === "?" && !entry) OS.key("help");
      else if (!entry && (NAV[e.key] || e.key === "`")) OS.key(NAV[e.key] || "back");
      else OS.key("char:" + e.key);
    } else handled = false;
    if (handled) { e.preventDefault(); paint(); }
  });
  // Guided tour: real key presses through the real screens.
  const T = (keys, d) => keys.split(" ").map(k => [k === "SP" ? "space" : k, d]);
  const TOUR = [
    ...T("/ . . , ; ;", 650), ["1", 1300], ["enter", 1300], ["`", 800], ["`", 900],
    ["2", 1300], ["enter", 1100], ...T("w h a t SP i s SP n e a r SP m e", 110), ["enter", 3600], ["`", 800], ["`", 900],
    ["9", 1200], ["enter", 1000], ...T("1 2 * 3 + 4", 160), ["enter", 1800], ["enter", 900], ...T("5 / 0", 200), ["enter", 1900], ["`", 900], ["`", 900],
    ["5", 1200], ["enter", 1100], ["enter", 1200], ["1", 1600], ["`", 600], ["`", 600], ["`", 800],
    ["3", 1200], ["enter", 2200], ["`", 700], ["`", 800], ["6", 2200], ["`", 900], ["G0", 3000], ["enter", 1000]];
  const tourBtn = document.getElementById("tour");
  function stopTour() { clearTimeout(tourTimer); tourBtn.setAttribute("aria-pressed", "false"); tourBtn.textContent = "Play the tour"; }
  function playTour(i = 0) {
    if (i === 0) { OS.state.locked = false; OS.key("home"); tourBtn.setAttribute("aria-pressed", "true"); tourBtn.textContent = "Stop the tour"; }
    if (i >= TOUR.length) { stopTour(); return; }
    const [k, d] = TOUR[i];
    if (k === "G0") { flash("G0"); OS.lock(); } else devKey(k);
    paint(); tourTimer = setTimeout(() => playTour(i + 1), d);
  }
  tourBtn.addEventListener("click", () => (tourBtn.getAttribute("aria-pressed") === "true" ? stopTour() : playTour(0)));
  // README captures (local only): ?bare&keys=1 enter&hold=enter
  if (qs.has("keys")) for (const k of qs.get("keys").split(" ").filter(Boolean)) (k === "G0" ? OS.lock() : devKey(k === "SP" ? "space" : k));
  if (HOLD) { document.querySelectorAll(".down").forEach(el => el.classList.remove("down")); for (const h of qs.get("hold").split(" ").filter(Boolean)) flash(h); }
  paint();
  if (location.hash === "#tour") playTour(0);
})();
