<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<!--
kuma3 (phones): the desktop "RWKV forecast" add-on's deck options part. Under desired
retention: this preset's review cards due / near / safe now at the current vs the selected
desired retention, and a dot bar (one dot per card at its recall now, red line at the selected
DR). Recall does not depend on the target, so typing a DR only moves the line. Every number
means "if you stop reviewing now". Recall comes from the backend (`rwkv_offline_forecast`
in rslib/src/scheduler/rwkv/offline.rs).

"minimum" (blue) = cards that are not due but are in today's reviews to reach "minimum reviews
per day"; "waiting" = due cards the deck list leaves out until enough other cards were answered
(minimum intervening reviews). For the current DR both come from the backend's own deck count;
for a typed DR the minimum is estimated (see minimumAt).
-->
<script lang="ts">
    import {
        RwkvOfflineForecastRequest,
        RwkvOfflineForecastResponse,
    } from "@generated/anki/scheduler_pb";
    import { postProto } from "@generated/post";

    /** The preset's name, escaped for a search. */
    export let presetSearch: string;
    /** The desired retention being typed. */
    export let selected: number;

    const NEAR_MARGIN = 0.03;
    const AXIS_MIN = 0.6;
    const DOT_STEP = 6; // px between stacked dots (less when the tallest stack passes MAX_HEIGHT)
    const MAX_HEIGHT = 70;
    const BIN = 0.006; // recall width of one stack (about one dot wide on a phone)

    type Card = { recall: number; target: number; minimum: boolean; waiting: boolean };
    let cards: Card[] | null = null;
    let status = "computing…";

    async function load(search: string): Promise<void> {
        try {
            const reply = await postProto(
                "rwkvOfflineForecast",
                new RwkvOfflineForecastRequest({
                    search: `preset:"${search}" is:review -is:learn -is:suspended -is:buried`,
                    offsetsSecs: [BigInt(0)],
                }),
                RwkvOfflineForecastResponse,
            );
            if (search !== presetSearch) {
                return; // the preset changed while this one was computing
            }
            if (!reply.available) {
                cards = null;
                status = "RWKV is not active on this device";
                return;
            }
            cards = reply.cards.map((c) => ({
                recall: c.recall[0],
                target: c.targetRetention,
                minimum: c.minimumToday,
                waiting: c.waiting,
            }));
            status = cards.length ? "" : "no RWKV review cards use this preset";
        } catch (error) {
            cards = null;
            status = `forecast unavailable: ${error}`;
        }
    }

    $: void load(presetSearch);

    /**
     * The cards "minimum reviews per day" adds at `target`. At the current DR, the backend's.
     * Otherwise: today's total (counted due + minimum) stays while the minimum is not reached,
     * filled with the lowest-recall cards that are not due at `target`.
     * ponytail: per preset, ignores parent decks' own minimums; exact only at the current DR.
     */
    function minimumAt(list: Card[], target: number): Set<Card> {
        if (list.every((c) => Math.abs(c.target - target) < 1e-4)) {
            return new Set(list.filter((c) => c.minimum));
        }
        const added = list.filter((c) => c.minimum).length;
        if (!added) {
            return new Set();
        }
        const counted = (t: (c: Card) => number) =>
            list.filter((c) => c.recall < t(c) && !c.waiting).length;
        const total = counted((c) => c.target) + added;
        const fill = Math.max(0, total - counted(() => target));
        const candidates = list
            .filter((c) => c.recall >= target)
            .sort((a, b) => a.recall - b.recall);
        return new Set(candidates.slice(0, fill));
    }

    type Kind = "due" | "minimum" | "near" | "safe";
    function kind(c: Card, target: number, minimum: Set<Card>): Kind {
        if (c.recall < target) {
            return "due";
        }
        if (minimum.has(c)) {
            return "minimum";
        }
        return c.recall < target + NEAR_MARGIN ? "near" : "safe";
    }

    function counts(list: Card[], target: number | null): Record<string, number> {
        const minimum = minimumAt(list, target ?? list[0].target);
        const out: Record<string, number> = { due: 0, minimum: 0, near: 0, safe: 0, waiting: 0 };
        for (const c of list) {
            const t = target ?? c.target;
            out[kind(c, t, minimum)] += 1;
            if (c.waiting && c.recall < t) {
                out.waiting += 1;
            }
        }
        return out;
    }

    const pct = (r: number) =>
        Math.max(0, Math.min(1, (r - AXIS_MIN) / (1 - AXIS_MIN))) * 100;

    type Dot = { left: number; bottom: number; kind: string };
    function layout(list: Card[], target: number): { dots: Dot[]; height: number } {
        const minimum = minimumAt(list, target);
        const sorted = [...list].sort((a, b) => a.recall - b.recall);
        const bins = sorted.map((c) => Math.floor(Math.max(c.recall, AXIS_MIN) / BIN));
        const sizes = new Map<number, number>();
        bins.forEach((b) => sizes.set(b, (sizes.get(b) ?? 0) + 1));
        const tallest = Math.max(1, ...sizes.values());
        const step = Math.min(DOT_STEP, (MAX_HEIGHT - 7) / Math.max(1, tallest - 1));
        const stacked = new Map<number, number>();
        const dots = sorted.map((c, i) => {
            const n = (stacked.get(bins[i]) ?? 0) + 1;
            stacked.set(bins[i], n);
            return {
                left: pct(c.recall),
                bottom: (n - 1) * step,
                kind: kind(c, target, minimum),
            };
        });
        return { dots, height: (tallest - 1) * step + 11 };
    }

    function dueCell(n: Record<string, number>): string {
        return n.waiting ? `${n.due} (${n.due - n.waiting} + ${n.waiting} waiting)` : `${n.due}`;
    }

    $: current = cards?.length ? counts(cards, null) : null;
    $: chosen = cards?.length ? counts(cards, selected) : null;
    $: currentLabel = cards
        ? [...new Set(cards.map((c) => `${+(c.target * 100).toFixed(2)}%`))].join("/")
        : "";
    $: bar = cards?.length ? layout(cards, selected) : null;
    const ticks = [0, 1, 2, 3, 4].map((k) => AXIS_MIN + ((1 - AXIS_MIN) * k) / 4);
</script>

<div class="rwkv-forecast ms-1 me-1">
    <div class="title">
        RWKV review cards now
        <span class="note">
            {#if cards?.length}
                ({cards.length} cards, if you stop reviewing now; near = within {NEAR_MARGIN *
                    100} points; minimum = added to reach minimum reviews per day)
            {:else}
                {status}
            {/if}
        </span>
    </div>
    {#if cards?.length && current && chosen}
        <table>
            <thead>
                <tr>
                    <th></th>
                    <th>Current DR ({currentLabel})</th>
                    <th>Selected DR ({(selected * 100).toFixed(2)}%)</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <th class="due">due</th>
                    <td>{dueCell(current)}</td>
                    <td>{dueCell(chosen)}</td>
                </tr>
                {#if current.minimum || chosen.minimum}
                    <tr>
                        <th class="minimum">minimum</th>
                        <td>{current.minimum}</td>
                        <td>{chosen.minimum}</td>
                    </tr>
                {/if}
                {#each ["near", "safe"] as k}
                    <tr>
                        <th class={k}>{k}</th>
                        <td>{current[k]}</td>
                        <td>{chosen[k]}</td>
                    </tr>
                {/each}
            </tbody>
        </table>
    {/if}
    {#if bar}
        <div class="bar" style="height: {bar.height}px">
            <div class="target" style="left: {pct(selected)}%"></div>
            {#each bar.dots as dot}
                <div
                    class="dot {dot.kind}"
                    style="left: {dot.left}%; bottom: {dot.bottom}px"
                ></div>
            {/each}
        </div>
        <div class="axis">
            {#each ticks as v}
                <span style="left: {pct(v)}%">{Math.round(v * 100)}%</span>
            {/each}
        </div>
    {/if}
</div>

<style>
    .rwkv-forecast {
        margin-bottom: 0.75rem;
    }

    .title {
        font-weight: 600;
        margin-bottom: 0.375rem;
    }

    .note {
        font-weight: normal;
        opacity: 0.6;
        font-size: 0.85em;
    }

    table {
        width: 100%;
        font-size: 0.9rem;
        border-collapse: collapse;
    }

    th,
    td {
        padding: 0.35rem 0.5rem;
        border: 1px solid var(--border);
        text-align: left;
        white-space: nowrap;
    }

    thead th {
        background: var(--canvas-elevated);
    }

    /* one colour per kind: the column heading's text and its dots */
    .due {
        --kind: #ef5350;
    }

    .near {
        --kind: #ffa726;
    }

    .safe {
        --kind: #66bb6a;
    }

    .minimum {
        --kind: #42a5f5;
    }

    th {
        color: var(--kind);
    }

    .bar {
        position: relative;
        margin: 0.8em 6px 0;
        border-bottom: 1px solid rgba(128, 128, 128, 0.5);
    }

    .dot {
        position: absolute;
        width: 7px;
        height: 7px;
        border-radius: 50%;
        margin-left: -3.5px;
        background: var(--kind);
    }

    .target {
        position: absolute;
        top: -4px;
        bottom: 0;
        width: 2px;
        margin-left: -1px;
        background: #ef5350;
    }

    .axis {
        position: relative;
        height: 14px;
        margin: 0 6px 0.8em;
        font-size: 10px;
        opacity: 0.6;
    }

    .axis span {
        position: absolute;
        transform: translateX(-50%);
        white-space: nowrap;
    }
</style>
