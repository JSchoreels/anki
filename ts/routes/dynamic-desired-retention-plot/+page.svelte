<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { onMount } from "svelte";

    import { bridgeCommand, bridgeCommandsAvailable } from "@tslib/bridgecommand";
    import DynamicDesiredRetentionPlotModal from "../deck-options/DynamicDesiredRetentionPlotModal.svelte";

    interface PlotPayload {
        params: number[];
        calibrationWeights: number[];
        calibrationAvgDrs: number[];
        fsrsEquivalentWeights: number[];
        fsrsEquivalentDrs: number[];
        retentionMin: number;
        retentionMax: number;
        targetAverageDr: number;
    }

    let payload: PlotPayload | null = null;
    let error = "";

    onMount(() => {
        const encoded = new URLSearchParams(window.location.search).get("payload");
        if (!encoded) {
            error = "Dynamic DR plot payload is missing.";
            return;
        }

        try {
            payload = JSON.parse(encoded) as PlotPayload;
        } catch (exc) {
            console.error(exc);
            error = "Dynamic DR plot payload is invalid.";
        }
    });

    function saveTarget(event: CustomEvent<number>): void {
        if (bridgeCommandsAvailable()) {
            bridgeCommand(`save:${event.detail}`);
        }
    }

    function closeDialog(): void {
        if (bridgeCommandsAvailable()) {
            bridgeCommand("dynamicDesiredRetentionPlotClose");
        }
    }
</script>

{#if payload}
    <DynamicDesiredRetentionPlotModal
        inline
        params={payload.params}
        calibrationWeights={payload.calibrationWeights}
        calibrationAvgDrs={payload.calibrationAvgDrs}
        fsrsEquivalentWeights={payload.fsrsEquivalentWeights}
        fsrsEquivalentDrs={payload.fsrsEquivalentDrs}
        retentionMin={payload.retentionMin}
        retentionMax={payload.retentionMax}
        targetAverageDr={payload.targetAverageDr}
        on:saveTarget={saveTarget}
        on:close={closeDialog}
    />
{:else if error}
    <main class="plot-error">{error}</main>
{/if}

<style>
    .plot-error {
        padding: 1rem;
        color: var(--fg);
    }
</style>
