<!--
Copyright: Ankitects Pty Ltd and contributors
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

    $: line = points.map((point) => `${x(point.seconds)},${y(point.recall)}`).join(" ");
</script>

<div class="answer-time-recall">
    <Graph title={tr.cardStatsRwkvAnswerTimeRecallTitle()}>
        <p class="description">{tr.cardStatsRwkvAnswerTimeRecallDescription()}</p>
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
            <polyline class="curve" points={line} />
            {#each points as point}
                <circle class="point" cx={x(point.seconds)} cy={y(point.recall)} r="4">
                    <title>{point.seconds}s: {percent(point.recall)}</title>
                </circle>
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
        <tbody>
            {#each points as point}
                <tr>
                    <th>{point.seconds}s</th>
                    <td>{percent(point.recall)}</td>
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
        stroke: var(--fg-link);
        stroke-width: 2.5;
    }
    .point {
        fill: var(--fg-link);
    }
</style>
