// Isolated benchmark: no installed add-on or production source is modified.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const repo = "/Users/jschoreels/workspace/Anki-Search-Stats-Extended";
const anki = "/Users/jschoreels/workspace/anki";
const inputs = "/private/tmp/anki-stats-audit-eqv1ucpr";
const { chromium } = require(anki + "/node_modules/playwright");
const esbuild = require(repo + "/node_modules/esbuild");
const fixture = JSON.parse(fs.readFileSync(inputs + "/fixture.json", "utf8"));
const fixedNow = new Date("2026-09-27T13:21:03Z").getTime();
const out = __dirname;
const bundlePath = inputs + "/benchmark-calculations.js";
esbuild.buildSync({
    stdin: {
        contents:
            `export {getMemorisedDays,buildCalibrationBins} from '${repo}/src/ts/MemorisedBar.ts'; export {calculateRevlogStats} from '${repo}/src/ts/revlogGraphs.ts';`,
        resolveDir: repo,
        loader: "ts",
    },
    bundle: true,
    platform: "browser",
    format: "iife",
    globalName: "SSEAudit",
    outfile: bundlePath,
    logLevel: "warning",
});

async function installHarness(page) {
    await page.evaluate(({ f, fixedNow }) => {
        window.fixture = f;
        window.fixedNow = fixedNow;
        Date.now = () => fixedNow;
        window.SSEconfig = {};
        window.SSEother = {
            rollover: f.rollover,
            deck_configs: f.configs,
            deck_config_ids: f.config_mapping,
            lang: "en",
            lang_ftl: "",
            fallback_ftl: "",
        };
        window.alert = () => {};
        console.warn = () => {};
    }, { f: fixture, fixedNow });
    await page.addScriptTag({ path: bundlePath });
    const bundle = fs.readFileSync(bundlePath, "utf8");
    return page.evaluate(async bundle => {
        window.calculate = async function(f, days) {
            const cutoff = Date.now() - f.rollover * 3600000 - days * 86400000;
            const reviews = days ? f.revlogs.filter(r => r.id > cutoff) : f.revlogs;
            const { calculateRevlogStats, getMemorisedDays, buildCalibrationBins } = SSEAudit;
            let start = performance.now();
            const rev = calculateRevlogStats(reviews, f.cards);
            const revlogMs = performance.now() - start;
            start = performance.now();
            const mem = await getMemorisedDays(
                reviews,
                f.cards,
                f.configs,
                f.config_mapping,
                rev.last_forget,
                2,
                2,
                f.presets,
            );
            const memorisedMs = performance.now() - start;
            start = performance.now();
            const bins = buildCalibrationBins(mem.calibration_samples, mem.fsrs_calibration_predictions_by_revlog_id);
            return {
                rev,
                mem,
                bins,
                timing: { reviews: reviews.length, revlogMs, memorisedMs, bootstrapMs: performance.now() - start },
            };
        };
        const bundleUrl = URL.createObjectURL(new Blob([bundle], { type: "text/javascript" }));
        const code = `
      let calculate;
      onmessage = async ({data}) => {
        try {
          if (data.init) {
            self.SSEconfig = {}; self.SSEother = data.other;
            Date.now = () => data.fixedNow; self.alert = () => {}; console.warn = () => {};
            importScripts(data.bundleUrl);
            calculate = (${window.calculate.toString()});
            postMessage({ready:true});
          } else {
            const result = await calculate(data.fixture, data.days);
            postMessage({result});
          }
        } catch (error) { postMessage({error: error.stack || String(error)}); }
      };
    `;
        const start = performance.now();
        window.worker = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
        await new Promise((resolve, reject) => {
            worker.onmessage = ({ data }) => data.error ? reject(new Error(data.error)) : resolve();
            worker.onerror = reject;
            worker.postMessage({ init: true, other: SSEother, fixedNow, bundleUrl });
        });
        window.lastResults = {};
        return { workerStartupMs: performance.now() - start };
    }, bundle);
}

async function runCalculation(page, variant, days) {
    return page.evaluate(async ({ variant, days }) => {
        let previous = performance.now(), ticks = 0, maxGap = 0;
        const heartbeat = setInterval(() => {
            const now = performance.now();
            maxGap = Math.max(maxGap, now - previous);
            previous = now;
            ticks++;
        }, 16);
        const start = performance.now();
        let result;
        if (variant === "main") {
            result = await calculate(fixture, days);
        } else {
            result = await new Promise((resolve, reject) => {
                worker.onmessage = ({ data }) => data.error ? reject(new Error(data.error)) : resolve(data.result);
                worker.onerror = reject;
                worker.postMessage({ fixture, days });
            });
        }
        const wallMs = performance.now() - start;
        const ticksDuringWork = ticks;
        await new Promise(resolve => setTimeout(resolve, 40));
        clearInterval(heartbeat);
        lastResults[variant] = result;
        return { variant, days, wallMs, ticksDuringWork, maxHeartbeatGapMs: maxGap, ...result.timing };
    }, { variant, days });
}

async function compareResults(page) {
    return page.evaluate(() => {
        let comparedValues = 0;
        // Enumerate array properties too: some legacy arrays have sparse/card-ID keys.
        function compare(a, b, where) {
            comparedValues++;
            if (Object.is(a, b)) { return; }
            if (!a || !b || typeof a !== "object" || typeof b !== "object") { throw new Error("Mismatch at " + where); }
            if (Array.isArray(a) !== Array.isArray(b)) { throw new Error("Type mismatch at " + where); }
            if (Array.isArray(a) && a.length !== b.length) { throw new Error("Length mismatch at " + where); }
            const ak = Object.keys(a), bk = Object.keys(b);
            if (ak.length !== bk.length) { throw new Error("Key count mismatch at " + where); }
            for (const k of ak) {
                if (!Object.hasOwn(b, k)) { throw new Error("Missing key at " + where + "." + k); }
                compare(a[k], b[k], where + "." + k);
            }
        }
        for (const k of ["rev", "mem", "bins"]) { compare(lastResults.main[k], lastResults.worker[k], k); }
        lastResults = {};
        return { equal: true, comparedValues };
    });
}

async function lightweightBenchmarks(page) {
    return page.evaluate(() => {
        function median(values) {
            return [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
        }
        function timed(fn) {
            const start = performance.now();
            const value = fn();
            return { ms: performance.now() - start, value };
        }
        const flat = JSON.stringify(fixture.presets);
        const unique = [], byValue = new Map(), assignments = {};
        for (const [cid, preset] of Object.entries(fixture.presets)) {
            const key = JSON.stringify(preset);
            if (!byValue.has(key)) {
                byValue.set(key, unique.length);
                unique.push(preset);
            }
            assignments[cid] = byValue.get(key);
        }
        const compact = JSON.stringify({ presets: unique, cards: assignments });
        const parseRows = [];
        for (let i = 0; i < 9; i++) {
            for (const variant of (i % 2 ? ["compact", "flat"] : ["flat", "compact"])) {
                const measured = timed(() => {
                    if (variant === "flat") { return JSON.parse(flat); }
                    const p = JSON.parse(compact), result = {};
                    for (const [cid, index] of Object.entries(p.cards)) { result[cid] = p.presets[index]; }
                    return result;
                });
                if (JSON.stringify(measured.value) !== flat) { throw new Error("Preset payload mismatch"); }
                parseRows.push({ variant, ms: measured.ms });
            }
        }
        const intervalRows = [];
        for (let i = 0; i < 21; i++) {
            const measured = timed(() => {
                const intervals = {};
                for (
                    const c of fixture.cards
                ) {
                    if ((c.type === 2 || c.type === 3) && c.queue !== -1) {
                        intervals[c.ivl] = (intervals[c.ivl] ?? 0) + 1;
                    }
                }
                return intervals;
            });
            intervalRows.push({ ms: measured.ms, bins: Object.keys(measured.value).length });
            window.derivedIntervals = measured.value;
        }
        return {
            presets: {
                flatBytes: new TextEncoder().encode(flat).length,
                compactBytes: new TextEncoder().encode(compact).length,
                unique: unique.length,
                rows: parseRows,
                flatMedianMs: median(parseRows.filter(r => r.variant === "flat").map(r => r.ms)),
                compactMedianMs: median(parseRows.filter(r => r.variant === "compact").map(r => r.ms)),
            },
            intervals: {
                rows: intervalRows,
                medianMs: median(intervalRows.map(r => r.ms)),
                histogram: derivedIntervals,
            },
        };
    });
}

(async () => {
    const browser = await chromium.launch({
        executablePath: anki
            + "/out/playwright-browsers/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell",
    });
    const results = { fixedNow, source: "copied backup 2026-09-27-15.21.03", rows: [] };
    try {
        const page = await browser.newPage();
        results.startup = await installHarness(page);
        results.lightweight = await lightweightBenchmarks(page);
        console.log("payload and interval measurements finished");
        for (const days of [365, 0]) {
            for (let round = 0; round < 3; round++) {
                const pair = [];
                for (const variant of (round % 2 ? ["worker", "main"] : ["main", "worker"])) {
                    const row = await runCalculation(page, variant, days);
                    row.round = round + 1;
                    pair.push(row);
                    console.log(JSON.stringify(row));
                }
                const check = await compareResults(page);
                results.rows.push({ days, round: round + 1, pair, check });
                fs.writeFileSync(path.join(out, "browser-results.json"), JSON.stringify(results, null, 2));
                console.log("complete result equality", JSON.stringify(check));
            }
        }
        assert.equal(results.rows.length, 6);
    } finally {
        await browser.close();
    }
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
