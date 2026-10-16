// SPDX-License-Identifier: GPL-3.0-or-later
// kuma3 XP theme, congrats page Easter egg: tap the window's red close box 5 times for a blue screen
// (ANKI_TOO_GOOD_EXCEPTION, a memory dump that counts up), then a "kuma3 XP" boot screen, then back to the page.
// A tap skips ahead. Injected with congrats.css by PageWebViewClient.
(function () {
    if (window.__xpEgg) return;
    window.__xpEgg = true;
    let taps = [];
    document.addEventListener("click", (e) => {
        const w = document.querySelector(".congrats");
        if (!w || document.getElementById("xp-egg")) return;
        const r = w.getBoundingClientRect();
        if (e.clientX < r.right - 34 || e.clientX > r.right + 4 || e.clientY < r.top || e.clientY > r.top + 30) return;
        const now = Date.now();
        taps = taps.filter((t) => now - t < 4000).concat(now);
        if (taps.length >= 5) {
            taps = [];
            bsod();
        }
    });

    function beep() {
        try {
            const ac = new AudioContext(), o = ac.createOscillator(), g = ac.createGain();
            o.type = "square";
            o.frequency.value = 220;
            g.gain.setValueAtTime(0.25, ac.currentTime);
            g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.5);
            o.connect(g).connect(ac.destination);
            o.start();
            o.stop(ac.currentTime + 0.5);
        } catch (e) {}
    }

    function layer(css) {
        const d = document.createElement("div");
        d.id = "xp-egg";
        d.style.cssText = "position:fixed;inset:0;z-index:2147483647;" + css;
        document.body.append(d);
        return d;
    }

    function bsod() {
        beep();
        const d = layer("background:#0000aa;color:#fff;font:14px/1.35 'Lucida Console','Courier New',monospace;padding:18px 14px;overflow:hidden;white-space:pre-wrap;font-size:13px");
        d.textContent = `A problem has been detected and kuma3 has been shut down to prevent damage to your memory.

ANKI_TOO_GOOD_EXCEPTION

If this is the first time you've seen this Stop error screen, study another deck. If this screen appears again, follow these steps:

Check to make sure every card has been reviewed. If this is a new deck, ask your teacher for more cards.

If problems continue, disable or remove any newly installed add-ons. Disable memory options such as forgetting.

Technical information:

*** STOP: 0x000000A7 (0x00000004, 0x0000RWKV, 0xC0FFEE00, 0xF5R57000)

***  kuma3.sys - Address BEA12000 base at BEA00000, DateStamp 6702e1a3

Beginning dump of physical memory
`;
        const dump = document.createTextNode("");
        d.append(dump);
        let p = 0;
        const t = setInterval(() => {
            p = Math.min(100, p + 5);
            dump.data = `Dumping physical memory to disk:  ${p}`;
            if (p === 100) {
                clearInterval(t);
                dump.data += "\nPhysical memory dump complete.\nContact your system administrator or technical support group for further\nassistance.";
                setTimeout(next, 1800);
            }
        }, 180);
        let done = false;
        const next = () => {
            if (done) return;
            done = true;
            clearInterval(t);
            d.remove();
            boot();
        };
        d.onclick = next;
    }

    function boot() {
        const d = layer("background:#000;display:grid;place-items:center;align-content:center;gap:48px;font-family:'XP Tahoma',Tahoma,sans-serif");
        d.innerHTML = `<div style="display:flex;align-items:center;gap:12px;color:#fff">
            <svg width="64" height="64" viewBox="0 0 20 20"><path fill="#f35325" d="M1 3L9 2V9.5L1 10Z"/><path fill="#81bc06" d="M10 1.9L19 1V9L10 9.5Z"/><path fill="#05a6f0" d="M1 11L9 10.5V18L1 19Z"/><path fill="#ffba08" d="M10 10.5L19 10V17L10 18Z"/></svg>
            <div style="line-height:1"><div style="font-size:15px;letter-spacing:1px">Anki</div>
            <div style="font:bold 44px 'XP Tahoma',Tahoma,sans-serif">kuma3<sup style="color:#ff7a00;font-size:24px">xp</sup></div></div></div>
            <div style="width:150px;height:16px;border:2px solid #b2b2b2;border-radius:5px;padding:2px;overflow:hidden;position:relative">
            <div id="xp-bar" style="position:absolute;top:2px;left:-40px;display:flex;gap:2px">
            <i style="width:9px;height:12px;background:linear-gradient(#2838c7,#7e95f9 50%,#2838c7)"></i>
            <i style="width:9px;height:12px;background:linear-gradient(#2838c7,#7e95f9 50%,#2838c7)"></i>
            <i style="width:9px;height:12px;background:linear-gradient(#2838c7,#7e95f9 50%,#2838c7)"></i></div></div>
            <div style="position:fixed;bottom:16px;left:16px;right:16px;display:flex;justify-content:space-between;color:#fff;font-size:12px">
            <span>Copyright © kuma3</span><span style="font-weight:bold;font-style:italic">Anki</span></div>`;
        const bar = d.querySelector("#xp-bar");
        let x = -40;
        const t = setInterval(() => {
            x = x > 160 ? -40 : x + 6;
            bar.style.left = x + "px";
        }, 40);
        let done = false;
        const next = () => {
            if (done) return;
            done = true;
            clearInterval(t);
            d.style.transition = "opacity .4s";
            d.style.opacity = "0";
            setTimeout(() => d.remove(), 400);
        };
        setTimeout(next, 4500);
        d.onclick = next;
    }
})();
