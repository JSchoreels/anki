const fs = require("node:fs");
const { chromium } = require("/Users/jschoreels/workspace/anki/node_modules/playwright");
const input = "/private/tmp/anki-stats-audit-eqv1ucpr";
(async () => {
    const browser = await chromium.launch({
        executablePath:
            "/Users/jschoreels/workspace/anki/out/playwright-browsers/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell",
    });
    try {
        const page = await browser.newPage();
        const result = await page.evaluate(({ flat, compact }) => {
            const normalized = JSON.stringify(JSON.parse(flat)), rows = [];
            for (let round = 0; round < 11; round++) {
                for (const variant of (round % 2 ? ["compact", "baseline"] : ["baseline", "compact"])) {
                    const start = performance.now();
                    let value;
                    if (variant === "baseline") { value = JSON.parse(flat); }
                    else {
                        const data = JSON.parse(compact);
                        value = {};
                        for (const [cid, index] of Object.entries(data.cards)) { value[cid] = data.presets[index]; }
                    }
                    const ms = performance.now() - start;
                    if (JSON.stringify(value) !== normalized) { throw new Error("Preset values differ"); }
                    rows.push({ variant, round: round + 1, ms, equal: true });
                }
            }
            return {
                baseline_bytes: new TextEncoder().encode(flat).length,
                compact_bytes: new TextEncoder().encode(compact).length,
                rows,
            };
        }, {
            flat: fs.readFileSync(input + "/baseline-presets.json", "utf8"),
            compact: fs.readFileSync(input + "/compact-presets.json", "utf8"),
        });
        fs.writeFileSync(__dirname + "/payload-results.json", JSON.stringify(result, null, 2));
        for (const variant of ["baseline", "compact"]) {
            const times = result.rows.filter(r => r.variant === variant).map(r => r.ms).sort((a, b) => a - b);
            console.log(
                JSON.stringify({
                    variant,
                    median_ms: times[Math.floor(times.length / 2)],
                    min_ms: times[0],
                    max_ms: times.at(-1),
                }),
            );
        }
    } finally {
        await browser.close();
    }
})().catch(e => {
    console.error(e);
    process.exitCode = 1;
});
