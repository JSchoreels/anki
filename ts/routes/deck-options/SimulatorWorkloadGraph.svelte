<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import AxisTicks from "../graphs/AxisTicks.svelte";
    import CumulativeOverlay from "../graphs/CumulativeOverlay.svelte";
    import HoverColumns from "../graphs/HoverColumns.svelte";
    import NoDataOverlay from "../graphs/NoDataOverlay.svelte";
    import { defaultGraphBounds } from "../graphs/graph-helpers";
    import {
        renderWorkloadChart,
        type SimulateWorkloadSubgraph,
        type WorkloadPoint,
    } from "../graphs/simulator";

    export let points: WorkloadPoint[];
    export let subgraph: SimulateWorkloadSubgraph;

    const bounds = {
        ...defaultGraphBounds(),
        marginRight: 180,
    };
    let svg: SVGElement | null = null;

    $: if (svg) {
        renderWorkloadChart(svg, bounds, points, subgraph);
    }
</script>

<section>
    <div class="svg-container">
        <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
            <CumulativeOverlay />
            <HoverColumns />
            <AxisTicks {bounds} />
            <NoDataOverlay {bounds} />
        </svg>
    </div>
</section>

<style>
    .svg-container {
        width: 100%;
        aspect-ratio: 600 / 250;
        display: flex;
        align-items: center;
    }

    svg {
        width: 100%;
        height: 100%;
    }
</style>
