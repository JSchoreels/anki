<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { GraphsResponse } from "@generated/anki/stats_pb";
    import * as tr from "@generated/ftl";
    import { createEventDispatcher } from "svelte";

    import AxisTicks from "./AxisTicks.svelte";
    import Graph from "./Graph.svelte";
    import { defaultGraphBounds } from "./graph-helpers";
    import type { GraphPrefs } from "./graph-helpers";
    import type { SearchEventMap, TableDatum } from "./graph-helpers";
    import HoverColumns from "./HoverColumns.svelte";
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import {
        gatherData,
        prepareData,
        type RetrievabilityHistogramData,
        retrievabilityHistogramGraph,
    } from "./retrievability";
    import TableData from "./TableData.svelte";
    import PercentageRange from "./PercentageRange.svelte";
    import { PercentageRangeEnum, PercentageRangeToQuantile } from "./percentageRange";

    export let sourceData: GraphsResponse | null = null;
    export let prefs: GraphPrefs;

    const dispatch = createEventDispatcher<SearchEventMap>();

    const bounds = defaultGraphBounds();
    let svg: HTMLElement | SVGElement | null = null;
    let histogramData: RetrievabilityHistogramData | null = null;
    let tableData: TableDatum[] = [];
    let range = PercentageRangeEnum.All;

    $: if (sourceData) {
        [histogramData, tableData] = prepareData(
            gatherData(sourceData),
            dispatch,
            $prefs.browserLinksSupported,
            PercentageRangeToQuantile(range),
        );
    }

    $: retrievabilityHistogramGraph(svg as SVGElement, bounds, histogramData);

    const title = tr.statisticsCardRetrievabilityTitle();
    const subtitle = tr.statisticsRetrievabilitySubtitle();
</script>

{#if sourceData?.fsrs}
    <Graph {title} {subtitle}>
        <PercentageRange bind:range />

        {#if histogramData && histogramData.series.length > 1}
            <div class="legend">
                {#each histogramData.series as series}
                    <span>
                        <span
                            class="swatch"
                            style={`background-color: ${series.colour}`}
                        ></span>
                        {series.label}
                    </span>
                {/each}
            </div>
        {/if}

        <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
            <g class="bars" />
            <HoverColumns />
            <AxisTicks {bounds} />
            <NoDataOverlay {bounds} />
        </svg>

        <TableData {tableData} />
    </Graph>
{/if}

<style lang="scss">
    .legend {
        display: flex;
        gap: 1rem;
        align-items: center;
        justify-content: center;
        margin-top: 0.25rem;
        font-size: 0.9rem;
    }

    .legend span {
        display: inline-flex;
        gap: 0.35rem;
        align-items: center;
    }

    .swatch {
        width: 0.8rem;
        height: 0.8rem;
        border-radius: 2px;
    }
</style>
