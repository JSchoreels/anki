/* kuma3: the Anki XP game in the desktop reviewer (aqt/xp/review.py drives it through window.kxp).
   The bar, the points × multiplier chips, the balloons and the skin's synthesized 8-bit sounds (WebAudio, no files),
   as the phone's XpReview / XpSound. */
(function () {
  "use strict";
  if (window.kxp) return;
  const $ = (id) => document.getElementById(id);
  let sound = true, limitMs = 10000, combo = 0;
  let clockTimer = null, clockStart = 0, lastSecond = 99;

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  // ------------------------------------------------------------ sounds (XpSound.kt)
  let ctx = null;
  function audio() {
    if (!sound) return null;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    } catch (e) { return null; }
  }
  /** notes [step] s apart, each [len] s with an exponential fade, like the skin's gain ramps */
  function notes(freqs, wave, vol, step, len) {
    const a = audio();
    if (!a || !freqs.length) return;
    const t0 = a.currentTime + 0.01;
    freqs.forEach((f, i) => {
      const o = a.createOscillator(), g = a.createGain();
      o.type = wave; o.frequency.value = f;
      const t = t0 + i * step;
      g.gain.setValueAtTime(vol * 0.5, t);
      g.gain.exponentialRampToValueAtTime(0.0005, t + len);
      o.connect(g).connect(a.destination);
      o.start(t); o.stop(t + len + 0.02);
    });
  }
  const BLIPS = { 1: [196, 131], 2: [440], 3: [523, 784], 4: [659, 880, 1319] };
  function blip(ease, c) {
    const up = Math.pow(2, Math.min(c, 12) / 12);
    notes((BLIPS[ease] || []).map((f) => f * up), "square", 0.22, 0.05, 0.09);
  }
  function chime(freqs, wave, vol) { notes(freqs, wave || "triangle", vol === undefined ? 0.35 : vol, 0.09, 0.3); }
  function slide() {
    const a = audio();
    if (!a) return;
    const t = a.currentTime + 0.01, o = a.createOscillator(), g = a.createGain();
    o.type = "sawtooth";
    o.frequency.setValueAtTime(440, t);
    o.frequency.exponentialRampToValueAtTime(110, t + 0.4);
    g.gain.setValueAtTime(0.17, t);
    g.gain.exponentialRampToValueAtTime(0.0005, t + 0.4);
    o.connect(g).connect(a.destination);
    o.start(t); o.stop(t + 0.42);
  }

  // ------------------------------------------------------------ the bar
  function init(opts) {
    sound = opts.sound !== false;
    limitMs = (opts.limit || 10) * 1000;
    document.body.classList.add("kxp");
    if ($("kxp-bar")) return;
    const bar = el("div", null,
      '<span id="kxp-goal"></span><span id="kxp-left"></span><span id="kxp-streak"></span><b id="kxp-lv"></b>' +
      '<div id="kxp-xp" title=""><div><i></i></div></div><span id="kxp-combo"></span>' +
      '<div id="kxp-clock"><i></i></div>');
    bar.id = "kxp-bar";
    bar.title = "Anki XP: click for today's quests and trophies";
    bar.onclick = () => window.pycmd && pycmd("kxp:quests");
    document.body.appendChild(bar);
    const fx = el("div"); fx.id = "kxp-fx"; document.body.appendChild(fx);
    const b = el("div"); b.id = "kxp-balloons"; document.body.appendChild(b);
  }

  /** s = {toGoal, left, streak, lv, frac, xp, next, combo, mult} */
  function update(s) {
    combo = s.combo;
    if (!$("kxp-bar")) return; // before the first card: init() makes it
    $("kxp-goal").innerHTML = s.toGoal > 0 ? "<b>" + s.toGoal + " to goal</b>" : '<b class="ok">goal ✓</b>';
    $("kxp-left").textContent = s.left >= 0 ? s.left + " left" : "";
    $("kxp-streak").textContent = "🔥" + s.streak;
    $("kxp-lv").textContent = "Lv " + s.lv;
    $("kxp-xp").querySelector("i").style.width = Math.round(s.frac * 100) + "%";
    $("kxp-xp").title = s.xp + " XP · " + s.next + " to level " + (s.lv + 1);
    const c = $("kxp-combo");
    const mult = s.combo >= 5 ? '<span class="mult">×' + s.mult + "</span> " : "";
    c.innerHTML = mult + "combo " + s.combo;
    c.className = s.combo >= 25 ? "blaze" : s.combo >= 10 ? "fire" : s.combo >= 5 ? "hot" : "";
  }

  /** the speed bonus clock along the bar's bottom: "wait" (blue: the word audio first), "run", "hide" */
  function clock(mode) {
    const c = $("kxp-clock"), fill = c.querySelector("i");
    clearInterval(clockTimer);
    c.style.display = mode === "hide" ? "none" : "block";
    c.className = mode === "wait" ? "wait" : "";
    fill.style.width = "100%";
    if (mode !== "run") return;
    clockStart = performance.now();
    lastSecond = 99;
    clockTimer = setInterval(() => {
      const left = Math.max(0, limitMs - (performance.now() - clockStart));
      fill.style.width = (100 * left / limitMs) + "%";
      c.className = left <= 3000 ? "red" : "";
      const sec = Math.ceil(left / 1000);
      if (left > 0 && left <= 3000 && sec < lastSecond && combo >= 3) chime([1200], "sine", 0.2);
      lastSecond = sec;
      if (left <= 0) clearInterval(clockTimer);
    }, 100);
  }

  // ------------------------------------------------------------ effects
  function balloon(title, msg) {
    const b = el("div", "kxp-balloon");
    b.appendChild(el("b", null)).textContent = title;
    b.appendChild(document.createTextNode(msg));
    $("kxp-balloons").appendChild(b);
    requestAnimationFrame(() => { b.style.opacity = 1; });
    setTimeout(() => { b.style.transition = "opacity .25s"; b.style.opacity = 0; setTimeout(() => b.remove(), 300); }, 3500);
  }

  function pop(text) {
    const p = el("div", "kxp-pop");
    p.textContent = text;
    document.body.appendChild(p);
    requestAnimationFrame(() => { p.style.transform = "translate(-50%, -50px)"; p.style.opacity = 0; });
    setTimeout(() => p.remove(), 950);
  }

  function multText(m) { return Number.isInteger(m) ? String(m) : m.toFixed(1); }

  /** the skin's Points × Mult: blue points and a red ×m slide in, slam into the gold total, count up, fly off */
  function chips(points, mult, xp) {
    const fx = $("kxp-fx");
    fx.innerHTML = "";
    const row = el("div", "kxp-chips");
    const p = el("span", "kxp-chip pts"); p.textContent = points;
    const m = el("span", "kxp-chip mul"); m.textContent = "×" + multText(mult);
    p.style.transform = "translateX(-60px)"; m.style.transform = "translateX(60px)";
    row.append(p, m);
    fx.appendChild(row);
    requestAnimationFrame(() => { p.style.transform = ""; m.style.transform = ""; });
    setTimeout(() => {
      row.innerHTML = "";
      const t = el("span", "kxp-chip tot");
      t.style.minWidth = (String(xp).length + 4) + "ch";
      row.appendChild(t);
      const t0 = performance.now();
      (function count() {
        const k = Math.min(1, (performance.now() - t0) / 300);
        t.textContent = "+" + Math.round(xp * (1 - Math.pow(1 - k, 3))) + " XP";
        if (k < 1) requestAnimationFrame(count);
      })();
      row.style.transition = "none"; row.style.transform = "scale(1.8)";
      requestAnimationFrame(() => { row.style.transition = "transform .4s"; row.style.transform = "scale(1)"; });
    }, 380);
    setTimeout(() => {
      row.style.transition = "transform .35s, opacity .35s";
      row.style.transform = "translateY(-40vh) scale(.5)"; row.style.opacity = 0;
      setTimeout(() => row.remove(), 400);
    }, 950);
  }

  function shake() {
    const bar = $("kxp-bar");
    bar.classList.remove("kxp-shake"); void bar.offsetWidth; bar.classList.add("kxp-shake");
  }

  function winSound(kind) {
    if (kind === "level") chime([523, 784, 1047, 1568]);
    else if (kind === "big") chime([659, 784, 988, 1319, 1568], "square");
    else if (kind === "combo") chime([523, 659, 784, 1047, 1319], "square");
    else chime([784, 988, 1175]);
  }

  /** r = {ease, points, mult, xp, broke, multUp, combo, wins: [[title, msg, kind]]} */
  function answered(r) {
    combo = r.combo;
    blip(r.ease, r.combo);
    chips(r.points, r.mult, r.xp);
    if (r.broke >= 3) { pop("COMBO BROKEN ×" + r.broke); slide(); }
    if (r.multUp) { chime([660, 880]); shake(); }
    r.wins.forEach((w, i) => setTimeout(() => { balloon(w[0], w[1]); winSound(w[2]); }, 250 * i));
  }

  window.kxp = { init, update, clock, answered, balloon, chime };
})();
