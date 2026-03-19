<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import SpinBoxRow from "./SpinBoxRow.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import Graph from "../graphs/Graph.svelte";
    import HoverColumns from "../graphs/HoverColumns.svelte";
    import CumulativeOverlay from "../graphs/CumulativeOverlay.svelte";
    import AxisTicks from "../graphs/AxisTicks.svelte";
    import NoDataOverlay from "../graphs/NoDataOverlay.svelte";
    import TableData from "../graphs/TableData.svelte";
    import InputBox from "../graphs/InputBox.svelte";
    import { defaultGraphBounds, type TableDatum } from "../graphs/graph-helpers";
    import {
        SimulateSubgraph,
        SimulateWorkloadSubgraph,
        type Point,
        type WorkloadPoint,
    } from "../graphs/simulator";
    import * as tr from "@generated/ftl";
    import { renderSimulationChart, renderWorkloadChart } from "../graphs/simulator";
    import {
        computeOptimalRetention,
        simulateFsrsReview,
        simulateFsrsWorkload,
    } from "@generated/backend";
    import { runWithBackendProgress } from "@tslib/progress";
    import type {
        ComputeOptimalRetentionResponse,
        SimulateFsrsReviewRequest,
        SimulateFsrsReviewResponse,
        SimulateFsrsWorkloadResponse,
    } from "@generated/anki/scheduler_pb";
    import type { DeckOptionsState } from "./lib";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import { reviewOrderChoices } from "./choices";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import { DeckConfig_Config_LeechAction } from "@generated/anki/deck_config_pb";
    import EasyDaysInput from "./EasyDaysInput.svelte";
    import Warning from "./Warning.svelte";
    import type { ComputeRetentionProgress } from "@generated/anki/collection_pb";
    import Modal from "bootstrap/js/dist/modal";
    import {
        buildFailPassRatioSeries,
        buildSLineSeries,
        median,
        matrixCellValue,
        rBucketLabel,
        sBucketLabel,
        seriesMinMax,
        type ReviewTimeMatrix,
    } from "./review-time-matrix";

    export let state: DeckOptionsState;
    export let simulateFsrsRequest: SimulateFsrsReviewRequest;
    export let computing: boolean;
    export let openHelpModal: (key: string) => void;
    export let onPresetChange: () => void;
    /** Do not modify this once set */
    export let workload: boolean = false;

    const config = state.currentConfig;
    let simulateSubgraph: SimulateSubgraph = SimulateSubgraph.count;
    let simulateWorkloadSubgraph: SimulateWorkloadSubgraph =
        SimulateWorkloadSubgraph.ratio;
    let tableData: TableDatum[] = [];
    let simulating: boolean = false;
    const fsrs = state.fsrs;
    const bounds = defaultGraphBounds();

    let svg: HTMLElement | SVGElement | null = null;
    let simulationNumber = 0;
    let points: (WorkloadPoint | Point)[] = [];
    let reviewTimeMatrix: ReviewTimeMatrix | undefined;
    let reviewTimeSampleMedian = 0;
    const newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;
    let smooth = true;
    let suspendLeeches = $config.leechAction == DeckConfig_Config_LeechAction.SUSPEND;
    let leechThreshold = $config.leechThreshold;

    let optimalRetention: null | number = null;
    let computingRetention = false;
    let computeRetentionProgress: ComputeRetentionProgress | undefined = undefined;

    $: daysToSimulate = 365;
    $: deckSize = 0;
    $: windowSize = Math.ceil(daysToSimulate / 365);
    $: processing = simulating || computingRetention;

    function movingAverage(y: number[], windowSize: number): number[] {
        const result: number[] = [];
        for (let i = 0; i < y.length; i++) {
            let sum = 0;
            let count = 0;
            for (let j = Math.max(0, i - windowSize + 1); j <= i; j++) {
                sum += y[j];
                count++;
            }
            result.push(sum / count);
        }
        return result;
    }

    function addArrays(arr1: number[], arr2: number[]): number[] {
        return arr1.map((value, index) => value + arr2[index]);
    }

    function estimatedRetention(retention: number): String {
        if (!retention) {
            return "";
        }
        return tr.deckConfigPredictedOptimalRetention({ num: retention.toFixed(2) });
    }

    function updateRequest() {
        simulateFsrsRequest.daysToSimulate = daysToSimulate;
        simulateFsrsRequest.deckSize = deckSize;
        simulateFsrsRequest.suspendAfterLapseCount = suspendLeeches
            ? leechThreshold
            : undefined;
        simulateFsrsRequest.easyDaysPercentages = easyDayPercentages;
    }

    function renderRetentionProgress(
        val: ComputeRetentionProgress | undefined,
    ): String {
        if (!val) {
            return "";
        }
        return tr.deckConfigIterations({ count: val.current });
    }

    $: computeRetentionProgressString = renderRetentionProgress(
        computeRetentionProgress,
    );

    async function computeRetention() {
        let resp: ComputeOptimalRetentionResponse | undefined;
        updateRequest();
        try {
            await runWithBackendProgress(
                async () => {
                    computingRetention = true;
                    resp = await computeOptimalRetention(simulateFsrsRequest);
                },
                (progress) => {
                    if (progress.value.case === "computeRetention") {
                        computeRetentionProgress = progress.value.value;
                    }
                },
            );
        } finally {
            computingRetention = false;
            if (resp) {
                optimalRetention = resp.optimalRetention;
            }
        }
    }

    async function simulateFsrs(): Promise<void> {
        let resp: SimulateFsrsReviewResponse | undefined;
        updateRequest();
        try {
            await runWithBackendProgress(
                async () => {
                    simulating = true;
                    resp = await simulateFsrsReview(simulateFsrsRequest);
                },
                () => {},
            );
        } finally {
            simulating = false;
            if (resp) {
                simulationNumber += 1;
                const dailyTotalCount = addArrays(
                    resp.dailyReviewCount,
                    resp.dailyNewCount,
                );

                const dailyMemorizedCount = resp.accumulatedKnowledgeAcquisition;

                points = points.concat(
                    resp.dailyTimeCost.map((v, i) => ({
                        x: i,
                        timeCost: v,
                        count: dailyTotalCount[i],
                        memorized: dailyMemorizedCount[i],
                        label: simulationNumber,
                    })),
                );

                tableData = renderSimulationChart(
                    svg as SVGElement,
                    bounds,
                    points,
                    simulateSubgraph,
                );
            }
        }
    }

    async function simulateWorkload(): Promise<void> {
        let resp: SimulateFsrsWorkloadResponse | undefined;
        updateRequest();
        try {
            await runWithBackendProgress(
                async () => {
                    simulating = true;
                    resp = await simulateFsrsWorkload(simulateFsrsRequest);
                },
                () => {},
            );
        } finally {
            simulating = false;
            if (resp) {
                simulationNumber += 1;
                reviewTimeMatrix = {
                    rBucketCount: resp.reviewTimeRBucketCount,
                    sBucketCount: resp.reviewTimeSBucketCount,
                    failSeconds: resp.reviewTimeFailSeconds,
                    passSeconds: resp.reviewTimePassSeconds,
                    sampleCounts: resp.reviewTimeSampleCounts,
                };

                points = points.concat(
                    Object.entries(resp.memorized).map(([dr, v]) => ({
                        x: parseInt(dr),
                        timeCost: resp!.cost[dr],
                        memorized: v,
                        start_memorized: resp!.startMemorized,
                        count: resp!.reviewCount[dr],
                        label: simulationNumber,
                        learnSpan: simulateFsrsRequest.daysToSimulate,
                    })),
                );

                tableData = renderWorkloadChart(
                    svg as SVGElement,
                    bounds,
                    points as WorkloadPoint[],
                    simulateWorkloadSubgraph,
                );
            }
        }
    }

    function clearSimulation() {
        points = points.filter((p) => p.label !== simulationNumber);
        simulationNumber = Math.max(0, simulationNumber - 1);
        reviewTimeMatrix = undefined;
        tableData = renderSimulationChart(
            svg as SVGElement,
            bounds,
            points,
            simulateSubgraph,
        );
    }

    function formatSeconds(seconds: number): string {
        return `${seconds.toFixed(1)}s`;
    }

    const graphWidth = 820;
    const graphHeight = 320;
    const graphMargin = { top: 18, right: 54, bottom: 46, left: 54 };
    const graphPassColor = "#2bb24c";
    const graphFailColor = "#e5484d";
    const graphRatioColor = "#ffffff";

    function graphX(rIndex: number, rBucketCount: number): number {
        const innerWidth = graphWidth - graphMargin.left - graphMargin.right;
        if (rBucketCount <= 1) {
            return graphMargin.left;
        }
        return graphMargin.left + (rIndex / (rBucketCount - 1)) * innerWidth;
    }

    function graphY(value: number, minValue: number, maxValue: number): number {
        const innerHeight = graphHeight - graphMargin.top - graphMargin.bottom;
        const ratio = (value - minValue) / Math.max(1e-6, maxValue - minValue);
        return graphMargin.top + (1 - ratio) * innerHeight;
    }

    function linePoints(
        values: number[],
        rBucketCount: number,
        minValue: number,
        maxValue: number,
    ): string {
        return values
            .map(
                (value, rIndex) =>
                    `${graphX(rIndex, rBucketCount)},${graphY(value, minValue, maxValue)}`,
            )
            .join(" ");
    }

    type ReviewTimeGraphLine = {
        color: string;
        kind: "pass" | "fail" | "ratio";
        points: string;
    };
    let reviewTimeGraphLines: ReviewTimeGraphLine[] = [];
    let reviewTimeGraphTimeYMin = 0;
    let reviewTimeGraphTimeYMax = 1;
    let reviewTimeGraphRatioYMin = 0;
    let reviewTimeGraphRatioYMax = 1;
    let reviewTimeGraphXTicks: { rIndex: number; label: string }[] = [];
    let reviewTimeGraphTimeYTicks: number[] = [];
    let reviewTimeGraphRatioYTicks: number[] = [];

    $: if (reviewTimeMatrix) {
        reviewTimeSampleMedian = median(reviewTimeMatrix.sampleCounts);
        const passLines = buildSLineSeries(
            reviewTimeMatrix.passSeconds,
            reviewTimeMatrix.rBucketCount,
            reviewTimeMatrix.sBucketCount,
        );
        const failLines = buildSLineSeries(
            reviewTimeMatrix.failSeconds,
            reviewTimeMatrix.rBucketCount,
            reviewTimeMatrix.sBucketCount,
        );
        const ratioLines = buildFailPassRatioSeries(
            reviewTimeMatrix.failSeconds,
            reviewTimeMatrix.passSeconds,
            reviewTimeMatrix.rBucketCount,
            reviewTimeMatrix.sBucketCount,
        );
        const [, passMax] = seriesMinMax(passLines);
        const [, failMax] = seriesMinMax(failLines);
        const [, ratioMax] = seriesMinMax(ratioLines);
        reviewTimeGraphTimeYMin = 0;
        reviewTimeGraphTimeYMax = Math.max(60, passMax, failMax);
        reviewTimeGraphRatioYMin = 0;
        reviewTimeGraphRatioYMax = Math.max(2, ratioMax);

        const graphLines: ReviewTimeGraphLine[] = [];
        for (let sIndex = 0; sIndex < reviewTimeMatrix.sBucketCount; sIndex++) {
            graphLines.push({
                color: graphPassColor,
                kind: "pass",
                points: linePoints(
                    passLines[sIndex],
                    reviewTimeMatrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphFailColor,
                kind: "fail",
                points: linePoints(
                    failLines[sIndex],
                    reviewTimeMatrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphRatioColor,
                kind: "ratio",
                points: linePoints(
                    ratioLines[sIndex],
                    reviewTimeMatrix.rBucketCount,
                    reviewTimeGraphRatioYMin,
                    reviewTimeGraphRatioYMax,
                ),
            });
        }
        reviewTimeGraphLines = graphLines;

        const xTickStep = Math.max(1, Math.floor(reviewTimeMatrix.rBucketCount / 6));
        reviewTimeGraphXTicks = Array.from(
            { length: reviewTimeMatrix.rBucketCount },
            (_, rIndex) => rIndex,
        )
            .filter(
                (rIndex) =>
                    rIndex % xTickStep === 0 ||
                    rIndex === reviewTimeMatrix.rBucketCount - 1,
            )
            .map((rIndex) => ({ rIndex, label: rBucketLabel(rIndex) }));

        reviewTimeGraphTimeYTicks = Array.from({ length: 5 }, (_, i) => {
            const ratio = i / 4;
            return (
                reviewTimeGraphTimeYMin +
                ratio * (reviewTimeGraphTimeYMax - reviewTimeGraphTimeYMin)
            );
        });

        reviewTimeGraphRatioYTicks = Array.from({ length: 5 }, (_, i) => {
            const ratio = i / 4;
            return (
                reviewTimeGraphRatioYMin +
                ratio * (reviewTimeGraphRatioYMax - reviewTimeGraphRatioYMin)
            );
        });
    }

    function saveConfigToPreset() {
        if (confirm(tr.deckConfigSaveOptionsToPresetConfirm())) {
            $config.newPerDay = simulateFsrsRequest.newLimit;
            $config.reviewsPerDay = simulateFsrsRequest.reviewLimit;
            $config.maximumReviewInterval = simulateFsrsRequest.maxInterval;
            if (!workload) {
                $config.desiredRetention = simulateFsrsRequest.desiredRetention;
            }
            $newCardsIgnoreReviewLimit = simulateFsrsRequest.newCardsIgnoreReviewLimit;
            $config.reviewOrder = simulateFsrsRequest.reviewOrder;
            $config.leechAction = suspendLeeches
                ? DeckConfig_Config_LeechAction.SUSPEND
                : DeckConfig_Config_LeechAction.TAG_ONLY;
            $config.leechThreshold = leechThreshold;
            $config.easyDaysPercentages = [...easyDayPercentages];
            onPresetChange();
        }
    }

    $: if (svg) {
        let pointsToRender = points;
        if (smooth) {
            // Group points by label (simulation number)
            const groupedPoints = points.reduce(
                (acc, point) => {
                    acc[point.label] = acc[point.label] || [];
                    acc[point.label].push(point);
                    return acc;
                },
                {} as Record<number, Point[]>,
            );

            // Apply smoothing to each group separately
            pointsToRender = Object.values(groupedPoints).flatMap((group) => {
                const smoothedTimeCost = movingAverage(
                    group.map((p) => p.timeCost),
                    windowSize,
                );
                const smoothedCount = movingAverage(
                    group.map((p) => p.count),
                    windowSize,
                );
                const smoothedMemorized = movingAverage(
                    group.map((p) => p.memorized),
                    windowSize,
                );

                return group.map((p, i) => ({
                    ...p,
                    timeCost: smoothedTimeCost[i],
                    count: smoothedCount[i],
                    memorized: smoothedMemorized[i],
                }));
            });
        }

        const render_function = workload ? renderWorkloadChart : renderSimulationChart;

        tableData = render_function(
            svg as SVGElement,
            bounds,
            // This cast shouldn't matter because we aren't switching between modes in the same modal
            pointsToRender as WorkloadPoint[],
            (workload ? simulateWorkloadSubgraph : simulateSubgraph) as any as never,
        );
    }

    $: easyDayPercentages = [...$config.easyDaysPercentages];

    export let modal: Modal | null = null;

    function setupModal(node: Element) {
        modal = new Modal(node);
        return {
            destroy() {
                modal?.dispose();
                modal = null;
            },
        };
    }
</script>

<div class="modal" tabindex="-1" use:setupModal>
    <div class="modal-dialog modal-xl">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">
                    {#if workload}
                        {tr.deckConfigFsrsSimulateDesiredRetentionExperimental()}
                    {:else}
                        {tr.deckConfigFsrsSimulatorExperimental()}
                    {/if}
                </h5>
                <button
                    type="button"
                    class="btn-close"
                    aria-label="Close"
                    on:click={() => modal?.hide()}
                ></button>
            </div>
            <div class="modal-body">
                <SpinBoxRow
                    bind:value={daysToSimulate}
                    defaultValue={365}
                    min={1}
                    max={Infinity}
                >
                    <SettingTitle on:click={() => openHelpModal("simulateFsrsReview")}>
                        {tr.deckConfigDaysToSimulate()}
                    </SettingTitle>
                </SpinBoxRow>

                <SpinBoxRow bind:value={deckSize} defaultValue={0} min={0} max={100000}>
                    <SettingTitle on:click={() => openHelpModal("simulateFsrsReview")}>
                        {tr.deckConfigAdditionalNewCardsToSimulate()}
                    </SettingTitle>
                </SpinBoxRow>

                {#if !workload}
                    <SpinBoxFloatRow
                        bind:value={simulateFsrsRequest.desiredRetention}
                        defaultValue={$config.desiredRetention}
                        min={0.1}
                        max={0.99}
                        percentage={true}
                    >
                        <SettingTitle
                            on:click={() => openHelpModal("desiredRetention")}
                        >
                            {tr.deckConfigDesiredRetention()}
                        </SettingTitle>
                    </SpinBoxFloatRow>
                {/if}

                <SpinBoxRow
                    bind:value={simulateFsrsRequest.newLimit}
                    defaultValue={$config.newPerDay}
                    min={0}
                    max={9999}
                >
                    <SettingTitle on:click={() => openHelpModal("newLimit")}>
                        {tr.schedulingNewCardsday()}
                    </SettingTitle>
                </SpinBoxRow>

                <SpinBoxRow
                    bind:value={simulateFsrsRequest.reviewLimit}
                    defaultValue={$config.reviewsPerDay}
                    min={0}
                    max={9999}
                >
                    <SettingTitle on:click={() => openHelpModal("reviewLimit")}>
                        {tr.schedulingMaximumReviewsday()}
                    </SettingTitle>
                </SpinBoxRow>

                <details>
                    <summary>{tr.deckConfigEasyDaysTitle()}</summary>
                    {#key easyDayPercentages}
                        <EasyDaysInput bind:values={easyDayPercentages} />
                    {/key}
                </details>

                <details>
                    <summary>{tr.deckConfigAdvancedSettings()}</summary>
                    <SpinBoxRow
                        bind:value={simulateFsrsRequest.maxInterval}
                        defaultValue={$config.maximumReviewInterval}
                        min={1}
                        max={36500}
                    >
                        <SettingTitle on:click={() => openHelpModal("maximumInterval")}>
                            {tr.schedulingMaximumInterval()}
                        </SettingTitle>
                    </SpinBoxRow>

                    <EnumSelectorRow
                        bind:value={simulateFsrsRequest.reviewOrder}
                        defaultValue={$config.reviewOrder}
                        choices={reviewOrderChoices($fsrs)}
                    >
                        <SettingTitle on:click={() => openHelpModal("reviewSortOrder")}>
                            {tr.deckConfigReviewSortOrder()}
                        </SettingTitle>
                    </EnumSelectorRow>

                    <SwitchRow
                        bind:value={simulateFsrsRequest.newCardsIgnoreReviewLimit}
                        defaultValue={$newCardsIgnoreReviewLimit}
                    >
                        <SettingTitle
                            on:click={() => openHelpModal("newCardsIgnoreReviewLimit")}
                        >
                            <GlobalLabel
                                title={tr.deckConfigNewCardsIgnoreReviewLimit()}
                            />
                        </SettingTitle>
                    </SwitchRow>

                    <SwitchRow bind:value={smooth} defaultValue={true}>
                        <SettingTitle
                            on:click={() => openHelpModal("simulateFsrsReview")}
                        >
                            {tr.deckConfigSmoothGraph()}
                        </SettingTitle>
                    </SwitchRow>

                    <SwitchRow
                        bind:value={suspendLeeches}
                        defaultValue={$config.leechAction ==
                            DeckConfig_Config_LeechAction.SUSPEND}
                    >
                        <SettingTitle on:click={() => openHelpModal("leechAction")}>
                            {tr.deckConfigSuspendLeeches()}
                        </SettingTitle>
                    </SwitchRow>

                    {#if suspendLeeches}
                        <SpinBoxRow
                            bind:value={leechThreshold}
                            defaultValue={$config.leechThreshold}
                            min={1}
                            max={9999}
                        >
                            <SettingTitle
                                on:click={() => openHelpModal("leechThreshold")}
                            >
                                {tr.schedulingLeechThreshold()}
                            </SettingTitle>
                        </SpinBoxRow>
                    {/if}
                </details>

                <div style="display:none;">
                    <details>
                        <summary>{tr.deckConfigComputeOptimalRetention()}</summary>
                        <button
                            class="btn {computingRetention
                                ? 'btn-warning'
                                : 'btn-primary'}"
                            disabled={!computingRetention && computing}
                            on:click={() => computeRetention()}
                        >
                            {#if computingRetention}
                                {tr.actionsCancel()}
                            {:else}
                                {tr.deckConfigComputeButton()}
                            {/if}
                        </button>

                        {#if optimalRetention}
                            {estimatedRetention(optimalRetention)}
                            {#if optimalRetention - $config.desiredRetention >= 0.01}
                                <Warning
                                    warning={tr.deckConfigDesiredRetentionBelowOptimal()}
                                    className="alert-warning"
                                />
                            {/if}
                        {/if}

                        {#if computingRetention}
                            <div>{computeRetentionProgressString}</div>
                        {/if}
                    </details>
                </div>

                <div>
                    <button
                        class="btn {computing ? 'btn-warning' : 'btn-primary'}"
                        disabled={computing}
                        on:click={workload ? simulateWorkload : simulateFsrs}
                    >
                        {tr.deckConfigSimulate()}
                    </button>

                    <button
                        class="btn {computing ? 'btn-warning' : 'btn-primary'}"
                        disabled={computing}
                        on:click={clearSimulation}
                    >
                        {tr.deckConfigClearLastSimulate()}
                    </button>

                    <button
                        class="btn {computing ? 'btn-warning' : 'btn-primary'}"
                        disabled={computing}
                        on:click={saveConfigToPreset}
                    >
                        {tr.deckConfigSaveOptionsToPreset()}
                    </button>

                    {#if processing}
                        {tr.actionsProcessing()}
                    {/if}
                </div>

                <Graph>
                    <div class="radio-group">
                        <InputBox>
                            {#if !workload}
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateSubgraph.count}
                                        bind:group={simulateSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioCount()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateSubgraph.time}
                                        bind:group={simulateSubgraph}
                                    />
                                    {tr.statisticsReviewsTimeCheckbox()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateSubgraph.memorized}
                                        bind:group={simulateSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioMemorized()}
                                </label>
                            {:else}
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.ratio}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioRatio2()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.count}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioCount()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.time}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.statisticsReviewsTimeCheckbox()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.memorized}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioMemorized()}
                                </label>
                            {/if}
                        </InputBox>
                    </div>

                    <div class="svg-container">
                        <svg
                            bind:this={svg}
                            viewBox={`0 0 ${bounds.width} ${bounds.height}`}
                        >
                            <CumulativeOverlay />
                            <HoverColumns />
                            <AxisTicks {bounds} />
                            <NoDataOverlay {bounds} />
                        </svg>
                    </div>

                    <TableData {tableData} />
                </Graph>

                {#if workload && reviewTimeMatrix}
                    <details class="review-time-matrix mt-2">
                        <summary>
                            {tr.statisticsReviewsTimeCheckbox()} Matrix (R/S, Fail/Pass)
                        </summary>
                        <div class="review-time-matrix-wrapper">
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>R \\ S</th>
                                        {#each Array.from({ length: reviewTimeMatrix.sBucketCount }) as _, sIndex}
                                            <th>{sBucketLabel(sIndex, reviewTimeMatrix.sBucketCount)}</th>
                                        {/each}
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each Array.from({ length: reviewTimeMatrix.rBucketCount }) as _, rIndex}
                                        <tr>
                                            <th>{rBucketLabel(rIndex)}</th>
                                            {#each Array.from({ length: reviewTimeMatrix.sBucketCount }) as _, sIndex}
                                                <td>
                                                    <div>F {formatSeconds(matrixCellValue(reviewTimeMatrix.failSeconds, rIndex, sIndex, reviewTimeMatrix.sBucketCount))}</div>
                                                    <div>P {formatSeconds(matrixCellValue(reviewTimeMatrix.passSeconds, rIndex, sIndex, reviewTimeMatrix.sBucketCount))}</div>
                                                    <div
                                                        class="review-time-samples {matrixCellValue(reviewTimeMatrix.sampleCounts, rIndex, sIndex, reviewTimeMatrix.sBucketCount) > reviewTimeSampleMedian
                                                            ? 'high'
                                                            : 'low'}"
                                                    >
                                                        n {matrixCellValue(reviewTimeMatrix.sampleCounts, rIndex, sIndex, reviewTimeMatrix.sBucketCount)}
                                                    </div>
                                                </td>
                                            {/each}
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-graph-wrapper">
                            <svg viewBox={`0 0 ${graphWidth} ${graphHeight}`}>
                                {#each reviewTimeGraphTimeYTicks as tick}
                                    {@const y = graphY(tick, reviewTimeGraphTimeYMin, reviewTimeGraphTimeYMax)}
                                    <line
                                        x1={graphMargin.left}
                                        x2={graphWidth - graphMargin.right}
                                        y1={y}
                                        y2={y}
                                        stroke="var(--border)"
                                        stroke-width="1"
                                    />
                                    <text
                                        x={graphMargin.left - 6}
                                        y={y}
                                        text-anchor="end"
                                        dominant-baseline="middle"
                                        class="review-time-axis-label"
                                    >
                                        {tick.toFixed(1)}s
                                    </text>
                                {/each}

                                {#each reviewTimeGraphRatioYTicks as tick}
                                    {@const y = graphY(tick, reviewTimeGraphRatioYMin, reviewTimeGraphRatioYMax)}
                                    <text
                                        x={graphWidth - graphMargin.right + 6}
                                        y={y}
                                        text-anchor="start"
                                        dominant-baseline="middle"
                                        class="review-time-axis-label review-time-axis-label-right"
                                    >
                                        {tick.toFixed(2)}
                                    </text>
                                {/each}

                                <line
                                    x1={graphMargin.left}
                                    x2={graphWidth - graphMargin.right}
                                    y1={graphHeight - graphMargin.bottom}
                                    y2={graphHeight - graphMargin.bottom}
                                    stroke="currentColor"
                                    stroke-width="1"
                                />
                                <line
                                    x1={graphWidth - graphMargin.right}
                                    x2={graphWidth - graphMargin.right}
                                    y1={graphMargin.top}
                                    y2={graphHeight - graphMargin.bottom}
                                    stroke="currentColor"
                                    stroke-width="1"
                                />

                                {#each reviewTimeGraphXTicks as tick}
                                    {@const x = graphX(tick.rIndex, reviewTimeMatrix.rBucketCount)}
                                    <line
                                        x1={x}
                                        x2={x}
                                        y1={graphHeight - graphMargin.bottom}
                                        y2={graphHeight - graphMargin.bottom + 4}
                                        stroke="currentColor"
                                        stroke-width="1"
                                    />
                                    <text
                                        x={x}
                                        y={graphHeight - graphMargin.bottom + 16}
                                        text-anchor="middle"
                                        class="review-time-axis-label"
                                    >
                                        {tick.label}
                                    </text>
                                {/each}

                                {#each reviewTimeGraphLines as line}
                                    <polyline
                                        class={`review-time-line ${line.kind}`}
                                        fill="none"
                                        stroke={line.color}
                                        stroke-width={line.kind === "ratio" ? 1.2 : 1.6}
                                        points={line.points}
                                    />
                                {/each}
                            </svg>
                        </div>

                        <div class="review-time-legend">
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line pass"></span>
                                <span>Pass time (left axis)</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line fail"></span>
                                <span>Fail time (left axis)</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line ratio"></span>
                                <span>Fail/Pass ratio (right axis)</span>
                            </div>
                        </div>
                    </details>
                {/if}
            </div>
        </div>
    </div>
</div>

<style>
    .modal {
        background-color: rgba(0, 0, 0, 0.5);
        --bs-modal-margin: 0;
    }

    .svg-container {
        width: 100%;
        /* Account for modal header, controls, etc */
        max-height: max(calc(100vh - 400px), 200px);
        aspect-ratio: 600 / 250;
        display: flex;
        align-items: center;
    }

    svg {
        width: 100%;
        height: 100%;
    }

    .modal-header {
        position: sticky;
        top: 0;
        background-color: var(--bs-body-bg);
        z-index: 100;
    }

    :global(.modal-xl) {
        max-width: 100vw;
    }

    div.radio-group {
        margin: 0.5em;
    }

    .btn {
        margin-bottom: 0.375rem;
    }

    summary {
        margin-bottom: 0.5em;
    }

    .review-time-matrix-wrapper {
        overflow: auto;
        max-height: 45vh;
        border: 1px solid var(--border);
    }

    .review-time-matrix-table {
        border-collapse: collapse;
        font-size: 0.78rem;
        white-space: nowrap;
        min-width: 100%;
    }

    .review-time-matrix-table th,
    .review-time-matrix-table td {
        border: 1px solid var(--border);
        padding: 0.2rem 0.35rem;
        text-align: right;
        vertical-align: top;
    }

    .review-time-matrix-table thead th {
        position: sticky;
        top: 0;
        background: var(--canvas-elevated);
        z-index: 1;
    }

    .review-time-graph-wrapper {
        border: 1px solid var(--border);
        overflow-x: auto;
        background: var(--canvas);
    }

    .review-time-graph-wrapper svg {
        min-width: 760px;
    }

    .review-time-axis-label {
        font-size: 0.7rem;
        fill: currentColor;
    }

    .review-time-axis-label-right {
        fill: color-mix(in srgb, currentColor 75%, transparent);
    }

    .review-time-line.fail {
        stroke-dasharray: 6 4;
    }

    .review-time-line.ratio {
        stroke-dasharray: 2.5 3;
        opacity: 0.85;
    }

    .review-time-legend {
        display: flex;
        flex-wrap: wrap;
        gap: 0.55rem 1rem;
        margin-top: 0.45rem;
        font-size: 0.76rem;
    }

    .review-time-legend-item {
        display: flex;
        align-items: center;
        gap: 0.35rem;
    }

    .review-time-legend-swatch {
        width: 0.95rem;
        height: 0.35rem;
        display: inline-block;
    }

    .review-time-legend-line {
        width: 1rem;
        border-top: 2px solid;
        display: inline-block;
    }

    .review-time-legend-line.pass {
        border-top-color: #2bb24c;
    }

    .review-time-legend-line.fail {
        border-top-color: #e5484d;
        border-top-style: dashed;
    }

    .review-time-legend-line.ratio {
        border-top-color: #ffffff;
        border-top-style: dotted;
    }

    .review-time-samples {
        font-size: 0.7rem;
        font-weight: 600;
    }

    .review-time-samples.high {
        color: var(--fg-green, #067647);
    }

    .review-time-samples.low {
        color: var(--fg-red, #b42318);
    }
</style>
