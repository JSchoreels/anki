<!--
Copyright: Anki Software, LLC and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { CardStatsResponse_AnswerTimeRecallPoint as Point } from "@generated/anki/stats_pb";
    import * as tr from "@generated/ftl";
    import Graph from "../graphs/Graph.svelte";

    export let points: Point[];

    const width = 640;
    const height = 300;
    const left = 55;
    const right = 20;
    const top = 18;
    const bottom = 52;
    const yTicks = [0, 0.25, 0.5, 0.75, 1];
    const x = (seconds: number): number =>
        left + (seconds / 30) * (width - left - right);
    const y = (recall: number): number => top + (1 - recall) * (height - top - bottom);
    const percent = (recall: number): string => `${Math.round(recall * 100)}%`;
    const tooltip = (label: string, point: Point, probability: number): string =>
        `${label} ${point.seconds}s: ${percent(probability)}`;

    const series = [
        { key: "recall", label: tr.cardStatsRwkvAnswerTimeSuccess() },
        { key: "hard", label: tr.cardStatsRwkvAnswerTimeHard() },
        { key: "good", label: tr.cardStatsRwkvAnswerTimeGood() },
        { key: "easy", label: tr.cardStatsRwkvAnswerTimeEasy() },
    ] as const;

    type SeriesKey = (typeof series)[number]["key"];

    function probability(point: Point, key: SeriesKey): number {
        if (key === "hard") {
            return point.recall;
        }
        if (key === "good") {
            // Separate protobuf floats can round their sum above total recall.
            return Math.min(point.recall, point.good + point.easy);
        }
        return point[key];
    }

    $: lines = series.map((item) => ({
        ...item,
        line: points
            .map((point) => `${x(point.seconds)},${y(probability(point, item.key))}`)
            .join(" "),
    }));
</script>

<div class="answer-time-recall">
    <Graph title={tr.cardStatsRwkvAnswerTimeRecallTitle()}>
        <p class="description">{tr.cardStatsRwkvAnswerTimeRecallDescription()}</p>
        <div class="legend">
            {#each lines as item}
                <span>
                    <span class={`swatch ${item.key}`}></span>
                    {item.label}
                </span>
            {/each}
        </div>
        <svg
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label={`${tr.cardStatsRwkvAnswerTimeRecallTitle()}: ${tr.cardStatsRwkvAnswerTimeRecallDescription()}`}
        >
            {#each yTicks as tick}
                <line
                    class="grid"
                    x1={left}
                    x2={width - right}
                    y1={y(tick)}
                    y2={y(tick)}
                />
                <text class="tick" x={left - 8} y={y(tick) + 4} text-anchor="end">
                    {percent(tick)}
                </text>
            {/each}
            {#each points as point}
                <text
                    class="tick"
                    x={x(point.seconds)}
                    y={height - bottom + 20}
                    text-anchor="middle"
                >
                    {point.seconds}
                </text>
            {/each}
            {#each lines as item}
                <polyline class={`curve ${item.key}`} points={item.line} />
                {#each points as point}
                    <circle
                        class={`point ${item.key}`}
                        cx={x(point.seconds)}
                        cy={y(probability(point, item.key))}
                        r={item.key === "recall" ? 5 : 4}
                    >
                        <title>
                            {tooltip(item.label, point, probability(point, item.key))}
                        </title>
                    </circle>
                {/each}
            {/each}
            <text
                class="axis-label"
                x={(left + width - right) / 2}
                y={height - 5}
                text-anchor="middle"
            >
                {tr.cardStatsRwkvAnswerTimeSecondsAxis()}
            </text>
            <text
                class="axis-label"
                transform={`translate(14 ${(top + height - bottom) / 2}) rotate(-90)`}
                text-anchor="middle"
            >
                {tr.cardStatsRwkvAnswerTimeRecallAxis()}
            </text>
        </svg>
    </Graph>
    <table class="visually-hidden">
        <caption>{tr.cardStatsRwkvAnswerTimeRecallTitle()}</caption>
        <thead>
            <tr>
                <th>{tr.cardStatsRwkvAnswerTimeSecondsAxis()}</th>
                {#each series as item}
                    <th>{item.label}</th>
                {/each}
            </tr>
        </thead>
        <tbody>
            {#each points as point}
                <tr>
                    <th>{point.seconds}s</th>
                    {#each series as item}
                        <td>{percent(probability(point, item.key))}</td>
                    {/each}
                </tr>
            {/each}
        </tbody>
    </table>
</div>

<style>
    .answer-time-recall {
        width: 100%;
        max-width: 50em;
        margin-bottom: 3em;
        color: var(--fg);
    }
    .answer-time-recall :global(.container) {
        box-sizing: border-box;
    }
    .description {
        text-align: center;
        opacity: 0.8;
    }
    .legend {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 0.5em 1.5em;
    }
    .swatch {
        display: inline-block;
        width: 1.5em;
        margin-inline-end: 0.4em;
        vertical-align: middle;
        border-top: 3px solid var(--series-color);
    }
    .recall {
        --series-color: var(--fg-link);
    }
    .hard {
        --series-color: #a37c00;
    }
    .good {
        --series-color: #15803d;
    }
    .easy {
        --series-color: #65a30d;
    }
    :global(.night-mode) .hard {
        --series-color: #facc15;
    }
    :global(.night-mode) .good {
        --series-color: #4ade80;
    }
    :global(.night-mode) .easy {
        --series-color: #bef264;
    }
    .swatch.good {
        border-top-style: dashed;
    }
    .swatch.hard {
        border-top-style: dashed;
    }
    .swatch.easy {
        border-top-style: dotted;
    }
    svg {
        width: 100%;
    }
    .grid {
        stroke: var(--fg);
        opacity: 0.12;
    }
    .tick,
    .axis-label {
        fill: var(--fg);
        font-size: 13px;
    }
    .tick {
        opacity: 0.7;
    }
    .curve {
        fill: none;
        stroke: var(--series-color);
        stroke-width: 2.5;
    }
    .curve.recall {
        stroke-width: 4;
    }
    .curve.good {
        stroke-dasharray: 8 4;
    }
    .curve.hard {
        stroke-dasharray: 8 3 2 3;
    }
    .curve.easy {
        stroke-dasharray: 2 4;
    }
    .point {
        fill: var(--series-color);
    }
</style>
