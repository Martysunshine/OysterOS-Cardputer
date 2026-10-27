// OysterOS screens and navigation, with the strings of firmware/src/app_controller.cpp and src/modules/*.cpp
// for one real device state: provisioned, Wi-Fi connected, PC backend off, direct AI key stored, no SD card.
// Anything that needs the radio, the IR LED, the microphone or the AI provider shows a notice instead of
// pretending to run.
const OS = (() => {
  const DEVICE_ONLY = "Needs the real Cardputer";
  const st = { battery: 87, session: "7c41e09a-001", brightness: 3, toast: "", toastUntil: 0, locked: false, lockFrame: 0,
    calc: { tape: [], expr: "", result: "", ok: true, count: 0 }, notes: "" };
  const KEYS = "Proposed keys, NOT yet confirmed on hardware:\nUp/Down: ; .  (Fn+; . anywhere)\nLeft/Right: , /  (page, adjust)\nSelect: Enter\nBack: ` or Del. A dialog is answered 'reject' first.\nHome: Fn+Del (press Fn first). Back to the root.\nHelp: ? (Shift+/). Not in text entry.\nCancel a task: Fn+` or Ctrl+C, on every screen.\nText entry: Del erases, Del on empty leaves, Fn+, / move the caret.\nHome screen: 1-9 open a tile.\nSettings > Key tester shows what each key does. Record results in docs/HARDWARE_VERIFICATION.md.";
  let stack = [];
  const top = () => stack[stack.length - 1];
  const open = s => { stack.push(s); s.onOpen && s.onOpen(); };
  const back = () => { if (stack.length > 1) stack.pop(); };
  const home = () => { stack = [stack[0]]; };
  const toast = t => { st.toast = t; st.toastUntil = performance.now() + 2600; };

  // ---- page builders ----
  function listPage(o) {
    return Object.assign({ kind: "list", sel: 0, top: 0, footer: ["Del:Back ?:Help", "Ent:Open"] }, o, {
      key(k) {
        const n = this.rows().length;
        if (k === "up" && this.sel > 0) this.sel--;
        else if (k === "down" && this.sel < n - 1) this.sel++;
        else if (k === "enter" && n) this.enter && this.enter(this.sel);
        else if (k === "del" || k === "back") back();
        else if (this.onKey) this.onKey(k);
        if (this.sel < this.top) this.top = this.sel;
        if (this.sel >= this.top + 5) this.top = this.sel - 4;
      },
      draw() { const rows = this.rows(); LCD.list({ title: this.titleText ? this.titleText() : this.title, rows, count: rows.length, sel: this.sel, top: this.top, disabled: this.disabled, empty: this.empty && this.empty() }); },
    });
  }
  function textPage(title, body, o = {}) {
    return Object.assign({ kind: "text", title, body, scroll: 0, left: "Del:Back ;.:Scroll" }, o, {
      lines() { return LCD.wrappedLines(typeof this.body === "function" ? this.body() : this.body); },
      key(k) {
        const max = Math.max(0, this.lines().length - 5);
        if (k === "up") this.scroll = Math.max(0, this.scroll - 1);
        else if (k === "down") this.scroll = Math.min(max, this.scroll + 1);
        else if (k === "left") this.scroll = Math.max(0, this.scroll - 5);
        else if (k === "right") this.scroll = Math.min(max, this.scroll + 5);
        else if (k === "del" || k === "back") back();
        else if (this.onKey) this.onKey(k);
      },
      footerText() { return [this.left, `${this.scroll + 1}/${Math.max(1, this.lines().length)}`]; },
      draw() { const L = this.lines(); LCD.message(typeof this.title === "function" ? this.title() : this.title, L.join("\n"), this.scroll, L.length); },
    });
  }
  function entryPage(prompt, max, submit, o = {}) {
    return Object.assign({ kind: "entry", buf: "", prompt, max }, o, {
      key(k) {
        if (k === "del") { if (this.buf) this.buf = this.buf.slice(0, -1); else back(); }
        else if (k === "enter") submit(this.buf, this);
        else if (k.startsWith("char:") && this.buf.length < this.max) this.buf += k.slice(5);
      },
      footerText() { return ["Ent:OK Del:Erase", `${this.buf.length}/${this.max}`]; },
      draw() { LCD.entry(this.prompt, this.buf, this.buf.length, "Del on empty text: Back"); },
    });
  }
  function helpFor(name, hint) {
    return textPage("Help", `${name}: ${hint}\nEnter: what the LED colours mean.\n\n${KEYS}`, {
      left: "Del:Back Ent:LED colours", onKey(k) { if (k === "enter") open(ledLegend()); } });
  }
  // kLegend (app_controller.cpp) with the status LED's own colours (status_led.cpp): label, comment, rgb, blink style
  const LEGEND = [["Online, breathing", "backend reachable", 0x00FF00, false], ["Attention, breathing", "confirm or new answer waits", 0x1E5AFF, false],
    ["Offline, blink every 2 s", "not set up or no backend", 0xFF6E00, true], ["Connecting, breathing", "joining Wi-Fi, reaching backend", 0xFF6E00, false],
    ["Error, blink every 1 s", "certificate, token or start-up", 0xFF0000, true], ["Scanning, breathing", "Wi-Fi survey or Bluetooth runs", 0x00E6C8, false],
    ["Boot, breathing", "start screen, press any key", 0xFFFFFF, false], ["Finished, 2 pulses", "an operation completed", 0x00FF00, true],
    ["Failed, 2 long blinks", "an operation failed", 0xFF0000, true], ["Notice, 3 quick blinks", "a request or answer arrived", 0x1E5AFF, true]];
  function ledLegend() {
    return { kind: "legend", sel: 0, top: 0, footer: ["Del:Back ;.:Move", ""], help: ["LED colours", "What the status LED shows. ; . move. Back returns."],
      key(k) { if (k === "up" && this.sel > 0) this.sel--; else if (k === "down" && this.sel < 9) this.sel++; else if (k === "del" || k === "back") back();
        if (this.sel < this.top) this.top = this.sel; if (this.sel >= this.top + 5) this.top = this.sel - 4; },
      draw() { LCD.colourTable("LED colours", LEGEND, this.sel, this.top); } };
  }

  // ---- Home ----
  const TILES = [
    { n: 1, label: "Security", icon: "Security", detail: "Wi-Fi, BLE", open: () => open(security()) },
    { n: 4, label: "Control", icon: "ControlDeck", detail: "offline", open: () => open(deck()) },
    { n: 2, label: "Oyster AI", icon: "OysterAi", detail: "offline", open: () => open(ai()) },
    { n: 5, label: "SmartHome", icon: "SmartHome", detail: "IR only", open: () => open(smartHome()) },
    { n: 3, label: "Red Team", icon: "RedTeam", detail: "scripts", open: () => open(redTeam()) },
    { n: 6, label: "Original", icon: "Apps", detail: "on PC", open: () => open(original()) },
    { n: 9, label: "Scratchpad", icon: "Scratchpad", detail: () => scratchStatus(), open: () => open(scratch()) },
    { n: 7, label: "Settings", icon: "Settings", detail: "device", open: () => open(settings()) },
    { n: 8, label: "Help", icon: null, detail: "keys", open: () => open(helpFor("Home [H01]", "Arrows move between tiles, Enter opens, 1-9 open directly.")) },
  ];
  const homePage = {
    kind: "tiles", sel: 0, top: 0, footer: ["?:Help 1-9:Open", "Ent:Open"],
    key(k) {
      const n = TILES.length;
      if (k === "right" && this.sel % 2 === 0 && this.sel + 1 < n) this.sel++;
      else if (k === "left" && this.sel % 2 === 1) this.sel--;
      else if (k === "down") this.sel = this.sel + 2 < n ? this.sel + 2 : this.sel % 2;
      else if (k === "up") this.sel = this.sel >= 2 ? this.sel - 2 : (this.sel % 2 === 0 ? 8 : 7);
      else if (k === "enter") TILES[this.sel].open();
      else if (k === "help") open(helpFor("Home [H01]", "Arrows move between tiles, Enter opens, 1-9 open directly."));
      else if (k.startsWith("digit:")) { const t = TILES.find(t => t.n === +k.slice(6)); if (t) { this.sel = TILES.indexOf(t); t.open(); } }
      const row = this.sel >> 1; if (row < this.top) this.top = row; if (row > this.top + 2) this.top = row - 2;
    },
    draw() {
      LCD.tiles({ title: "OYSTER OS", right: `${TILES[this.sel].n}/9`, sel: this.sel, top: this.top,
        tiles: TILES.map(t => ({ icon: t.icon, label: t.label, enabled: true, detail: `${t.n} ${String(typeof t.detail === "function" ? t.detail() : t.detail).slice(0, 11)}` })) });
    },
  };

  // ---- Security ----
  function security() {
    const items = [["Wi-Fi Survey", "passive 2.4 GHz survey", wifiSurvey], ["Bluetooth Explorer", "advertisement discovery", bleExplorer],
      ["Saved Findings", "evidence log and upload state", evidence], ["Enrolled Targets", "0 enrolled (your own devices)", targets],
      ["Assess (GATT)", "authorized GATT checks", assess]];
    const icons = ["Wifi", "Ble", "Findings", "Targets", "Assess"];
    return { kind: "tiles", sel: 0, top: 0, footer: ["Del:Back ?:Help", "Ent:Open"], help: ["Security [S10]", "Pick a tool. Enter opens it."],
      key(k) {
        if (k === "down" && this.sel < 4) this.sel++; else if (k === "up" && this.sel > 0) this.sel--;
        else if (k === "enter") open(items[this.sel][2]()); else if (k === "del" || k === "back") back();
        if (this.sel < this.top) this.top = this.sel; if (this.sel > this.top + 2) this.top = this.sel - 2;
      },
      draw() { LCD.tiles({ title: "Security", right: `${this.sel + 1}/5`, sel: this.sel, top: this.top, cols: 1,
        tiles: items.map((it, i) => ({ icon: icons[i], label: it[0], detail: it[1], enabled: true })) }); } };
  }
  const radio = () => toast(DEVICE_ONLY);
  const wifiSurvey = () => listPage({ title: "Wi-Fi Survey", help: ["Wi-Fi Survey [S11]", "Start a passive 2.4 GHz survey, then open the results. The room label is stored with it."],
    rows: () => ["Start survey (passive)", "Results (0)", "Live scan (wardrive)", "Room: (none)"], enter: i => (i === 1 ? open(wifiResults()) : radio()) });
  const wifiResults = () => listPage({ title: "Wi-Fi results", footer: ["Del:Back s:Sort", "Ent:Info"], rows: () => [], empty: () => "(empty)",
    help: ["Wi-Fi results [S12]", "Enter: detail. s: sort by RSSI or channel. , /: page."] });
  const bleExplorer = () => listPage({ title: "Bluetooth Explorer", help: ["Bluetooth Explorer [S21]", "Passive discovery only listens; active discovery sends scan requests."],
    rows: () => ["Discover (passive)", "Discover (active)", "Results (0)"], enter: i => (i === 2 ? open(listPage({ title: "BLE results", footer: ["Del:Back ?:Help", "Ent:Info"], rows: () => [], help: ["BLE results [S22]", "Enter: detail. , /: page."] })) : radio()) });
  const evidence = () => textPage("Saved Findings", () => `Saved findings = the evidence log of this session.\nSession: sess-${st.session}\nBoot: ${st.session.slice(0, 8)}\nDevice: cardputer-01\nStorage: volatile RAM ring (lost on reset)\nSD state: not_present\nEvents written (last seq): 3\nAccepted by backend, highest seq: 0\nSkipped (refused by backend): 0\nAwaiting upload: 3\nDropped events: 0\nDropped UI events: 0\nLast operation: none\nBackend: offline\nEvents of an earlier boot stay on the SD card but are NOT uploaded after a reboot (known gap, milestone M6). Upload covers this session only.\nFindings are evaluated on the backend (Ask Oyster or the report).`,
    { help: ["Saved Findings", "Upload and session state of the evidence log. ; . scroll."] });
  const targets = () => listPage({ title: "Targets (hint)", footer: ["Del:Back a:Assess", "Ent:Info"], rows: () => [], empty: () => "No enrolled targets",
    help: ["Enrolled Targets", "Enter: detail. a: assess the selected target."] });
  const READS = [["public_sensor", "pub"], ["restricted_enc", "enc"], ["restricted_mitm", "mitm"], ["app_auth", "pub"], ["diag_manifest", "app"],
    ["chunk_data", "app"], ["diag_status", "pub"], ["virtual_led", "pub"], ["device_info", "pub"]];
  const M4 = ["Access-policy check", "Observe notifications", "Write lab value", "Diagnostic transfer"];
  function assess() {
    const rows = ["Target: (none enrolled)", "Inspect GATT", ...READS.map(r => `Read ${r[0]} [${r[1]}]`), "GATT results (0)", "Last read result", ...M4.map(m => `${m}: not in build`)];
    const disabled = rows.map((_, i) => i >= rows.length - 4);
    return listPage({ title: "Assess", footer: ["Del:Back ?:Help", "Ent:Run"], rows: () => rows, disabled,
      help: ["Assess", "Enter runs the selected check on the enrolled target. , /: target."], enter: () => radio() });
  }

  // ---- Oyster AI (direct mode) ----
  const IDLE = "Type a question to ask the provider directly.\nThis mode is plain Q&A: no evidence or findings are used, and nothing is sent to a backend.";
  const runHeader = "Oyster AI (direct, no backend)\nModel: (default)\n\n";
  function runView(asking) {
    const p = textPage("Oyster AI (direct)", () => runHeader + (p.asking ? "Asking the provider over Wi-Fi...\nThis usually takes a few seconds." : IDLE),
      { asking, help: ["Oyster AI run [A11/A12]", "Status and answer pages. ; . scroll."] });
    if (asking) setTimeout(() => { p.asking = false; toast(DEVICE_ONLY); }, 2200);
    return p;
  }
  const ai = () => listPage({ title: "Oyster AI (direct)", help: ["Oyster AI [A10]", "Type or speak (v) a question, read the latest run, cancel it or sync."],
    rows: () => ["Ask a question", "Speak a question (v)", "Latest AI run", "Cancel AI run", "Sync evidence now"],
    enter(i) {
      if (i === 0) open(entryPage("Ask Oyster", 240, (q) => { if (!q) { back(); return; } stack.pop(); open(runView(true)); }));
      else if (i === 2) open(runView(false));
      else if (i === 3) toast("No AI run to cancel");
      else radio();
    },
    onKey(k) { if (k === "char:v" || k === "char:V") radio(); } });

  // ---- Control Deck (backend off) ----
  function deck() {
    const p = listPage({ title: "Control Deck", loading: true, rows: () => [],
      empty: () => (p.loading ? "Loading profiles..." : "Backend offline. r:Retry"),
      footerText() { return [p.loading ? "Working... Del:Back" : "Ent:Open r:Refresh Del:Back", ""]; },
      onKey(k) { if (k === "char:r" || k === "char:R") fetch(); },
      help: ["Control Deck", "Enter opens the selected row. Back goes up one level. r refreshes where offered."] });
    const fetch = () => { p.loading = true; setTimeout(() => { p.loading = false; }, 1300); };
    fetch(); return p;
  }

  // ---- Smart Home (backend off) ----
  const BRANDS = ["TV Samsung", "TV LG", "TV Sony", "TV NEC (Toshiba type)", "TV Philips (RC5)"];
  const FUNCS = ["Power", "Vol+", "Vol-", "Mute", "Ch+", "Ch-"];
  const SH_HELP = ["Smart Home", "Enter opens the selected row. Back goes up one level. r refreshes where offered."];
  const smartHome = () => listPage({ title: "Smart Home", footer: ["Ent:Open r:Refresh", ""], help: SH_HELP,
    rows: () => ["IR Remote (5 TVs)", "Home Assistant: no backend"], disabled: [false, true],
    enter(i) {
      if (i === 1) open(textPage("Home Assistant: no backend", "Not connected to the PC backend, so Home Assistant cannot be asked.\nCheck Settings > Backend, then press r on the room list.\nIR Remote works without it.", { help: SH_HELP }));
      else open(listPage({ title: "IR Remote", footer: ["Ent:Open Del:Back", ""], help: SH_HELP, rows: () => [...BRANDS, "About IR codes"],
        enter(b) {
          if (b === 5) open(textPage("About IR codes", "The codes are fixed tables for common TV remote families (NEC, Samsung32, Sony SIRC, RC5) from public protocol documentation.\nThey are NOT verified on your devices, and the IR LED pin (GPIO44) is unverified on this unit.\nIR is one-way: nothing comes back, so this screen cannot know whether the TV obeyed.\nPoint the device at the TV. There is no learn mode (no IR receiver).", { help: SH_HELP }));
          else open(listPage({ title: BRANDS[b], footer: ["Ent/1-6:Send Del:Back", ""], help: SH_HELP, rows: () => FUNCS.map((f, j) => `${j + 1} ${f}`),
            enter: () => radio(), onKey(k) { if (/^char:[1-6]$/.test(k) || /^digit:[1-6]$/.test(k)) radio(); } }));
        } }));
    },
    onKey(k) { if (k === "char:r" || k === "char:R") toast("Backend offline"); } });

  // ---- Red Team (Scripts) ----
  const ABOUT_RT = "Scripting runs on the device with the laptop off (PikaScript).\nBindings: o = Oyster.Oyster(), then o.log(text), o.sleep_ms(ms), o.wifi_ap_count().\nAdd your own .py files under /oyster/scripts on a microSD card and they appear here.\n\nFull offensive Wi-Fi firmware for this board is a separate project you flash yourself:\nEvil-M5Project by 7h30th3r0n3\ngithub.com/7h30th3r0n3/Evil-M5Project\n\nIts attacks (deauth, captive portals, Karma, beacon spam) are not built into Oyster.";
  const RT_HELP = ["Scripts", "Enter opens the selected row. Back goes up one level. r refreshes where offered."];
  const redTeam = () => listPage({ title: "Red Team", footer: ["Ent:Open Del:Back", ""], help: RT_HELP,
    rows: () => ["About / Evil-M5 reference", "Run example (built-in)"],
    enter(i) {
      if (i === 0) open(textPage("Red Team", ABOUT_RT, { left: "Del:Back", help: RT_HELP }));
      else open(textPage("example", "Hello from Oyster scripting.\nCounting Wi-Fi APs...\n0\nDone.\n", { left: "Del:Back", help: RT_HELP }));
    } });

  // ---- Original firmware (backup slot present on this unit) ----
  const original = () => textPage("Original firmware", "Kept in a backup slot:\ncardputer-adv\nV0.9-36-ge824a76\nBuilt Oct 21 2025\nIDF v5.4.2\n\nIt cannot boot from here: it was built with a newer IDF than this device's bootloader, which only loads one firmware.\n\nTo run it, on the PC:\n  scripts/original-restore.ps1\n(restores it from your verified backup; it then runs with its own bootloader).\nTo return to Oyster:\n  scripts/firmware-flash.ps1",
    { help: ["Original Apps", "Enter opens the selected row. Back goes up one level. r refreshes where offered."] });

  // ---- Scratchpad (works fully here: calculator and notes run in the page) ----
  function scratchStatus() {
    const c = st.calc.count, n = st.notes.length > 0;
    if (!c && !n) return "calc+notes";
    return c ? `${c} calc` : "notes";
  }
  const SC_HELP = ["Scratchpad", "Enter opens the selected row. Back goes up one level. r refreshes where offered."];
  function calcEntry() {
    return entryPage("= expression", 48, (expr, page) => {
      if (!expr.trim()) { back(); return; }
      const r = Calc.evaluate(expr), c = st.calc;
      c.expr = expr; c.ok = r.ok; c.result = r.ok ? Calc.format(r.value) : r.err; c.count++;
      c.tape.unshift(r.ok ? `${expr} = ${c.result}` : `${expr}  (${c.result})`); c.tape = c.tape.slice(0, 9);
      stack.pop(); open(calcTape());
    }, { help: SC_HELP });
  }
  const calcTape = () => ({ kind: "calc", footer: ["Ent:New Del:Menu", ""], help: SC_HELP,
    key(k) { if (k === "enter") { stack.pop(); open(calcEntry()); } else if (k === "del" || k === "back") back(); },
    draw() { const c = st.calc; LCD.calc({ title: "Calculator", expr: c.expr, result: c.result, ok: c.ok, prev: c.tape[1] || "" }); } });
  const notesView = () => textPage("Notes", () => st.notes || "No notes yet.\nEnter adds a line.", { left: "Ent:Add Del:Menu", help: SC_HELP,
    onKey(k) { if (k === "enter") open(entryPage("note", 63, (t) => { if (!t.trim()) { back(); return; } st.notes += (st.notes ? "\n" : "") + t; back(); }, { help: SC_HELP })); } });
  const scratch = () => listPage({ title: "Scratchpad", footer: ["Ent:Open", ""], help: SC_HELP, rows: () => ["Calculator", "Notes", "Clear all"],
    enter(i) {
      if (i === 0) open(st.calc.count ? calcTape() : calcEntry());
      else if (i === 1) open(notesView());
      else { st.calc = { tape: [], expr: "", result: "", ok: true, count: 0 }; st.notes = ""; toast("Cleared"); }
    } });

  // ---- Settings ----
  const ST_HELP = ["Settings [P10]", "Enter opens an entry. , / adjust the brightness."];
  const settings = () => listPage({ title: "Settings", footer: ["Del:Back ,/:Adjust", "Ent:Open"], help: ST_HELP,
    rows: () => ["Wi-Fi: setup", "Backend: offline", `Brightness: ${st.brightness}/5`, "Storage: not_present", "Diagnostics", "Sync evidence now", "New session", "Keyboard", "Key tester", "About"],
    onKey(k) { if (this.sel === 2 && (k === "left" || k === "right")) { st.brightness = Math.max(1, Math.min(5, st.brightness + (k === "right" ? 1 : -1))); } },
    enter(i) {
      const H = { help: ST_HELP };
      if (i === 1) open(textPage("Backend", "State: offline\nHost: 192.168.1.50:8443\nWi-Fi SSID: (your network)\nDevice id: cardputer-01\nProvisioned: yes\nCA certificate: 1233 bytes\nEnrolled targets: 0\nToken and Wi-Fi password are never shown. Provision over USB: scripts/provision.ps1.", H));
      else if (i === 2) toast(", / adjust the brightness");
      else if (i === 3) open(textPage("Storage", "SD state: not_present\nCard type: 0\nTotal: 0 MB\nUsed: 0 MB\nEvidence: volatile RAM ring (bounded)\nA card inserted after boot is used after a reboot.", H));
      else if (i === 4) open(textPage("Diagnostics", () => `Startup: all modules started\nEvidence store: volatile RAM (no SD)\nUptime: ${Math.floor(performance.now() / 1000)} s\nHeap free: 56 KiB, largest block 47 KiB\nReset: power on\nRadio: idle\nBattery: ${st.battery}%`, H));
      else if (i === 7) open(textPage("Keyboard", KEYS, H));
      else if (i === 9) open(textPage("About", "Oyster Pocket firmware 0.1.0\nBoard id: 24 (Cardputer ADV)\nFlash: 8 MB, PSRAM: none\nWi-Fi MAC: (hidden here)\nReset reason: power on\nKeys: see Settings > Keyboard (proposed mapping, not yet confirmed on hardware).", H));
      else radio();
    } });

  // ---- input and drawing ----
  stack = [homePage];
  function key(k) {
    if (st.locked) { st.locked = false; return; }
    const s = top();
    if (k === "home") { home(); return; }
    if (k === "help" && s.kind !== "entry") {
      if (s === homePage) return homePage.key("help");
      if (s.title === "Help") return;
      const h = s.help || ["This screen", "Back returns."]; open(helpFor(h[0], h[1])); return;
    }
    if (s.kind === "entry") {
      if (k.startsWith("char:") || k === "del" || k === "enter") s.key(k);
      else if (k === "back") s.key("char:`");
      return;
    }
    if (k.startsWith("char:") && /^char:[1-9]$/.test(k) && s === homePage) return s.key("digit:" + k.slice(5));
    s.key(k);
  }
  function draw() {
    if (st.locked) { LCD.lockScreen(st.lockFrame, st.battery); return; }
    LCD.status({ mode: "ONLINE", session: st.session, backend: "offline", battery: st.battery, conn: "off", wifi: "on" });
    const s = top(); s.draw();
    if (performance.now() < st.toastUntil) LCD.notice(st.toast);
    else { const f = s.footerText ? s.footerText() : s.footer; LCD.footer(f[0], f[1]); }
  }
  function lock() { st.locked = true; st.lockFrame = 0; }
  function tick() { if (st.locked) st.lockFrame = (st.lockFrame + 1) % 8; }
  function where() {
    if (st.locked) return "Lock screen";
    const s = top(); return s === homePage ? "Home" : (typeof s.title === "function" ? s.title() : s.title) || (s.kind === "calc" ? "Calculator" : s.prompt || "Screen");
  }
  return { key, draw, lock, tick, where, state: st, isEntry: () => top().kind === "entry" };
})();
