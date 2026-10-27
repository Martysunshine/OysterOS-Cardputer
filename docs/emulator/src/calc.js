// Port of firmware/lib/oyster_core/src/calc_eval.cpp: recursive descent, + - * / % ( ) and unary +/-.
const Calc = (() => {
  function evaluate(expr) {
    const s = { p: 0, ok: true, err: null, t: expr };
    const ws = () => { while (s.t[s.p] === " " || s.t[s.p] === "\t") s.p++; };
    const fail = m => { if (s.ok) { s.ok = false; s.err = m; } };
    function primary() {
      ws();
      if (s.t[s.p] === "(") { s.p++; const v = exp(); ws(); if (s.t[s.p] === ")") s.p++; else fail("Missing )"); return v; }
      const m = /^(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?/.exec(s.t.slice(s.p));
      if (!m) { fail("Bad expression"); return 0; }
      s.p += m[0].length; return parseFloat(m[0]);
    }
    function factor() { ws(); if (s.t[s.p] === "+") { s.p++; return factor(); } if (s.t[s.p] === "-") { s.p++; return -factor(); } return primary(); }
    function term() {
      let v = factor();
      for (;;) { if (!s.ok) return 0; ws(); const c = s.t[s.p];
        if (c === "*") { s.p++; v *= factor(); }
        else if (c === "/" || c === "%") { s.p++; const r = factor(); if (r === 0) { fail("Divide by zero"); return 0; } v = c === "/" ? v / r : v % r; }
        else break; }
      return v;
    }
    function exp() {
      let v = term();
      for (;;) { if (!s.ok) return 0; ws(); const c = s.t[s.p];
        if (c === "+") { s.p++; v += term(); } else if (c === "-") { s.p++; v -= term(); } else break; }
      return v;
    }
    if (!expr || !expr.trim()) return { ok: false, err: "Empty" };
    const v = exp();
    if (!s.ok) return { ok: false, err: s.err || "Bad expression" };
    ws(); if (s.p < s.t.length) return { ok: false, err: "Bad expression" };
    if (!isFinite(v)) return { ok: false, err: "Out of range" };
    return { ok: true, value: v };
  }
  // printf("%.10g"), with -0 folded to "0".
  function format(v) {
    if (v === 0) return "0";
    const e = Math.floor(Math.log10(Math.abs(Number(v.toPrecision(10)))));
    const strip = s => s.includes(".") ? s.replace(/\.?0+$/, "") : s;
    if (e < -4 || e >= 10) {
      let [m, x] = v.toExponential(9).split("e"); m = strip(m);
      const n = parseInt(x, 10); return `${m}e${n < 0 ? "-" : "+"}${String(Math.abs(n)).padStart(2, "0")}`;
    }
    return strip(v.toFixed(Math.max(0, 9 - e)));
  }
  return { evaluate, format };
})();
