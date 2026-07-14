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
        renderSimulationChart,
        renderWorkloadChart,
        smoothPointsByLabel,
        type Point,
        type WorkloadComparisonEngine,
        type WorkloadPoint,
        workloadSameMemorizedSavings,
    } from "../graphs/simulator";
    import { SimulateFsrsWorkloadResponse } from "@generated/anki/scheduler_pb";
    import type {
        ComputeOptimalRetentionResponse,
        SimulateFsrsReviewResponse,
    } from "@generated/anki/scheduler_pb";
    import type { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";
    import type { DeckOptionsState } from "./lib";
    import * as tr from "@generated/ftl";
    import {
        computeOptimalRetention,
        simulateFsrsReview,
        simulateFsrsWorkload,
    } from "@generated/backend";
    import { postProto } from "@generated/post";
    import { runWithBackendProgress } from "@tslib/progress";
    import {
        DeckConfig_Config_LeechAction,
        DeckConfig_Config_FsrsVersion,
        type DeckConfig,
        type DeckConfig_Config,
    } from "@generated/anki/deck_config_pb";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import { reviewOrderChoices } from "./choices";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import EasyDaysInput from "./EasyDaysInput.svelte";
    import Warning from "./Warning.svelte";
    import { ComputeRetentionProgress } from "@generated/anki/collection_pb";
    import { Empty } from "@generated/anki/generic_pb";
    import { workloadRequestForPreset } from "./simulator-workload";
    import Modal from "bootstrap/js/dist/modal";
    import {
        buildSLineSeries,
        median,
        matrixCellValue,
        rBucketLabel,
        sBucketLabel,
        seriesMinMax,
        type ReviewTimeMatrix,
    } from "./review-time-matrix";
    import {
        HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
        HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
    } from "./help-me-decide-defaults";
    import SimulatorWorkloadGraph from "./SimulatorWorkloadGraph.svelte";

    export let state: DeckOptionsState;
    export let simulateFsrsRequest: SimulateFsrsReviewRequest;
    export let computing: boolean;
    export let openHelpModal: (key: string) => void;
    export let onPresetChange: () => void;
    /** Do not modify this once set */
    export let workload: boolean = false;
    /** Do not modify this once set */
    export let rwkvWorkload: boolean = false;
    /** Run FSRS and RWKV from the same settings and compare their charts. */
    export let compareWorkloads: boolean = false;

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
    let fsrsComparisonPoints: WorkloadPoint[] = [];
    let rwkvComparisonPoints: WorkloadPoint[] = [];
    let reviewTimeMatrix: ReviewTimeMatrix | undefined;
    let reviewTimeAgainCoeffs: number[] = [];
    let reviewTimeHardCoeffs: number[] = [];
    let reviewTimeGoodCoeffs: number[] = [];
    let reviewTimeEasyCoeffs: number[] = [];
    let reviewTimeGradeWeights: number[] = [];
    let reviewTimeTransitionProbs: number[] = [];
    let reviewTimeTransitionCounts: number[] = [];
    let reviewTimeSuccessGradeProbs: number[] = [];
    let reviewTimeSuccessGradeCounts: number[] = [];
    let reviewTimeSampleMedian = 0;
    const newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;
    const RWKV_WORKLOAD_SAMPLE_LIMIT_DEFAULT = 250;
    const RWKV_WORKLOAD_TARGET_STEP_DEFAULT = 1;
    const RWKV_WORKLOAD_STATE_UPDATE_INTERVAL_DEFAULT = 10;
    const RWKV_WORKLOAD_RESULT_POLL_MS = 500;
    let smooth = true;
    let suspendLeeches = $config.leechAction == DeckConfig_Config_LeechAction.SUSPEND;
    let leechThreshold = $config.leechThreshold;

    let optimalRetention: null | number = null;
    let computingRetention = false;
    let computeRetentionProgress: ComputeRetentionProgress | undefined = undefined;
    let workloadProgress: ComputeRetentionProgress | undefined = undefined;
    let workloadProgressPollPending = false;
    let transitionBlendAlpha =
        simulateFsrsRequest.helpMeDecideTransitionBlendAlpha ??
        HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT;
    let enforceMonotonicSuccessGradeProbs =
        simulateFsrsRequest.helpMeDecideEnforceMonotonicSuccessGradeProbs ??
        HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT;
    let simulateDynamicDesiredRetention =
        simulateFsrsRequest.simulateDynamicDesiredRetention;
    let splitWorkloadByPreset = simulateFsrsRequest.splitWorkloadByPreset;
    let rwkvWorkloadSampleLimit =
        simulateFsrsRequest.rwkvWorkloadSampleLimit ||
        (rwkvWorkload && !compareWorkloads ? RWKV_WORKLOAD_SAMPLE_LIMIT_DEFAULT : 0);
    let rwkvWorkloadTargetStep =
        simulateFsrsRequest.rwkvWorkloadTargetStep ||
        (rwkvWorkload ? RWKV_WORKLOAD_TARGET_STEP_DEFAULT : 1);
    let rwkvWorkloadStateUpdateInterval =
        simulateFsrsRequest.rwkvWorkloadStateUpdateInterval ||
        (rwkvWorkload && !compareWorkloads
            ? RWKV_WORKLOAD_STATE_UPDATE_INTERVAL_DEFAULT
            : 1);
    let dynamicDesiredRetentionAvailable = false;

    $: daysToSimulate = 365;
    $: deckSize = 0;
    $: windowSize = smoothingWindowSize();
    $: processing = simulating || computingRetention;
    $: fsrsComparisonRenderPoints = smooth
        ? smoothPointsByLabel(fsrsComparisonPoints, windowSize)
        : fsrsComparisonPoints;
    $: rwkvComparisonRenderPoints = smooth
        ? smoothPointsByLabel(rwkvComparisonPoints, windowSize)
        : rwkvComparisonPoints;
    $: comparisonRenderPoints = [
        ...fsrsComparisonRenderPoints,
        ...rwkvComparisonRenderPoints,
    ];

    function smoothingWindowSize(): number {
        if (rwkvWorkload) {
            return 7;
        }
        if (workload) {
            return 5;
        }
        return Math.ceil(daysToSimulate / 365);
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
        simulateFsrsRequest.helpMeDecideTransitionBlendAlpha = transitionBlendAlpha;
        simulateFsrsRequest.helpMeDecideEnforceMonotonicSuccessGradeProbs =
            enforceMonotonicSuccessGradeProbs;
        simulateFsrsRequest.simulateDynamicDesiredRetention =
            !rwkvWorkload &&
            simulateDynamicDesiredRetention &&
            dynamicDesiredRetentionAvailable;
        simulateFsrsRequest.splitWorkloadByPreset = workload && splitWorkloadByPreset;
        simulateFsrsRequest.rwkvWorkloadSampleLimit = rwkvWorkload
            ? rwkvWorkloadSampleLimit
            : 0;
        simulateFsrsRequest.rwkvWorkloadTargetStep = rwkvWorkload
            ? rwkvWorkloadTargetStep
            : 1;
        simulateFsrsRequest.rwkvWorkloadStateUpdateInterval = rwkvWorkload
            ? rwkvWorkloadStateUpdateInterval
            : 1;
    }

    function subtreeConfigs(): DeckConfig[] {
        const subtreeIds = new Set(state.getSubtreeConfigIds());
        return Array.from(subtreeIds)
            .map((id) => state.getConfigById(id))
            .filter((config): config is DeckConfig => config !== undefined);
    }

    function workloadRequests(
        engine: "fsrs" | "rwkv" = rwkvWorkload ? "rwkv" : "fsrs",
    ): {
        name: string;
        request: SimulateFsrsReviewRequest;
    }[] {
        return subtreeConfigs()
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((config) => {
                const request = workloadRequestForPreset(
                    simulateFsrsRequest,
                    state.getCurrentDeckNameForSearch(),
                    config,
                );
                return {
                    name: workloadRunName(config.name, request, engine),
                    request,
                };
            });
    }

    function workloadRunName(
        presetName: string,
        request: SimulateFsrsReviewRequest,
        engine: "fsrs" | "rwkv",
    ): string {
        if (engine === "rwkv") {
            return `${presetName} (RWKV)`;
        }
        return `${presetName} (${request.simulateDynamicDesiredRetention ? "ADR" : "Fixed DR"})`;
    }

    function supportsDynamicDesiredRetentionSimulation(
        config: DeckConfig_Config | undefined,
    ): boolean {
        return config?.fsrsVersion === DeckConfig_Config_FsrsVersion.SEVEN;
    }

    function hasDynamicDesiredRetention(config: DeckConfig): boolean {
        return supportsDynamicDesiredRetentionSimulation(config.config);
    }

    $: dynamicDesiredRetentionAvailable =
        !rwkvWorkload &&
        (workload
            ? Boolean($config) && subtreeConfigs().some(hasDynamicDesiredRetention)
            : supportsDynamicDesiredRetentionSimulation($config));
    $: if (!dynamicDesiredRetentionAvailable) {
        simulateDynamicDesiredRetention = false;
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
    $: workloadProgressPct = renderWorkloadProgressPct(workloadProgress);
    $: workloadProgressString = renderWorkloadProgress(
        workloadProgress,
        workloadProgressPct,
    );

    function renderWorkloadProgressPct(
        val: ComputeRetentionProgress | undefined,
    ): number | undefined {
        if (!val || !val.total) {
            return undefined;
        }
        return Math.min(100, Math.max(0, (val.current / val.total) * 100));
    }

    function renderWorkloadProgress(
        val: ComputeRetentionProgress | undefined,
        pct: number | undefined,
    ): string {
        if (!val || pct === undefined) {
            return "";
        }
        return `RWKV workload: ${pct.toFixed(1)}% (${val.current}/${val.total})`;
    }

    async function updateRwkvWorkloadProgress(): Promise<void> {
        if (workloadProgressPollPending) {
            return;
        }
        workloadProgressPollPending = true;
        try {
            const progress = await postProto(
                "rwkvWorkloadProgress",
                new Empty({}),
                ComputeRetentionProgress,
                { alertOnError: false },
            );
            if (simulating && progress.total) {
                workloadProgress = progress;
            }
        } catch {
            // The simulation result request will surface real errors.
        } finally {
            workloadProgressPollPending = false;
        }
    }

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

    type NamedWorkloadResponse = {
        name: string;
        response: SimulateFsrsWorkloadResponse;
    };

    function workloadPointsFromResponses(
        responses: NamedWorkloadResponse[],
        runNumber: number,
        comparisonEngine?: WorkloadComparisonEngine,
    ): WorkloadPoint[] {
        let labelOffset = 0;
        return responses.flatMap(({ name, response }, responseIndex) => {
            const workloads = response.presetWorkload.length
                ? response.presetWorkload.map((preset) => ({
                      name: `${preset.name} (${name})`,
                      memorized: preset.memorized,
                      weightedMemorized: preset.weightedMemorized,
                      reviewlessEndMemorized: preset.reviewlessEndMemorized,
                      reviewlessEndWeightedMemorized:
                          preset.reviewlessEndWeightedMemorized,
                      cost: preset.cost,
                      reviewCount: preset.reviewCount,
                      learnCount: preset.learnCount,
                  }))
                : [
                      {
                          name,
                          memorized: response.memorized,
                          weightedMemorized: response.weightedMemorized,
                          reviewlessEndMemorized: {},
                          reviewlessEndWeightedMemorized: {},
                          cost: response.cost,
                          reviewCount: response.reviewCount,
                          learnCount: {},
                      },
                  ];
            return workloads.flatMap((workload, workloadIndex) => {
                labelOffset += 1;
                const label = comparisonEngine
                    ? runNumber * 1000 +
                      labelOffset * 2 +
                      (comparisonEngine === "rwkv" ? 1 : 0)
                    : runNumber * 1000 + labelOffset;
                const comparisonLabel = comparisonEngine
                    ? workload.name.replace(/\s+\((?:Fixed DR|RWKV)\)(\))?$/, "$1")
                    : undefined;
                return Object.entries(workload.memorized)
                    .filter(
                        ([dr]) =>
                            workload.reviewCount[dr] ||
                            workload.learnCount[dr] ||
                            workload.cost[dr],
                    )
                    .map(([dr, memorized]) => ({
                        x: parseInt(dr),
                        timeCost: workload.cost[dr],
                        memorized,
                        weightedMemorized: workload.weightedMemorized[dr],
                        reviewless_end_memorized:
                            workload.reviewlessEndMemorized[dr] ??
                            response.reviewlessEndMemorized,
                        reviewless_end_weighted_memorized:
                            workload.reviewlessEndWeightedMemorized[dr] ??
                            response.reviewlessEndWeightedMemorized,
                        count:
                            (workload.reviewCount[dr] ?? 0) +
                            (workload.learnCount[dr] ?? 0),
                        label,
                        labelName: workload.name,
                        learnSpan: simulateFsrsRequest.daysToSimulate,
                        comparisonEngine,
                        comparisonKey: comparisonEngine
                            ? `${runNumber}:${responseIndex}:${workloadIndex}`
                            : undefined,
                        comparisonLabel,
                    }));
            });
        });
    }

    async function simulateWorkload(): Promise<void> {
        const responses: NamedWorkloadResponse[] = [];
        const fsrsResponses: NamedWorkloadResponse[] = [];
        const rwkvResponses: NamedWorkloadResponse[] = [];
        updateRequest();
        try {
            await runWithBackendProgress(
                async () => {
                    simulating = true;
                    workloadProgress = undefined;
                    if (compareWorkloads) {
                        for (const { name, request } of workloadRequests("fsrs")) {
                            fsrsResponses.push({
                                name,
                                response: await simulateFsrsWorkload(request),
                            });
                        }
                        for (const { name, request } of workloadRequests("rwkv")) {
                            rwkvResponses.push({
                                name,
                                response: await simulateRwkvWorkload(request),
                            });
                        }
                    } else {
                        for (const { name, request } of workloadRequests()) {
                            const response = rwkvWorkload
                                ? await simulateRwkvWorkload(request)
                                : await simulateFsrsWorkload(request);
                            responses.push({ name, response });
                        }
                    }
                },
                () => {
                    if (rwkvWorkload) {
                        void updateRwkvWorkloadProgress();
                    }
                },
            );
        } finally {
            simulating = false;
            if (fsrsResponses.length && rwkvResponses.length) {
                simulationNumber += 1;
                fsrsComparisonPoints = fsrsComparisonPoints.concat(
                    workloadPointsFromResponses(
                        fsrsResponses,
                        simulationNumber,
                        "fsrs",
                    ),
                );
                rwkvComparisonPoints = rwkvComparisonPoints.concat(
                    workloadPointsFromResponses(
                        rwkvResponses,
                        simulationNumber,
                        "rwkv",
                    ),
                );
            }
            if (responses.length) {
                simulationNumber += 1;
                const runNumber = simulationNumber;
                const responseWithSamples = responses.find(
                    ({ response }) => response.reviewTimeSampleCounts.length,
                );
                const firstResponse =
                    responseWithSamples?.response ?? responses[0].response;
                reviewTimeMatrix = {
                    rBucketCount: firstResponse.reviewTimeRBucketCount,
                    sBucketCount: firstResponse.reviewTimeSBucketCount,
                    againSeconds: firstResponse.reviewTimeAgainSeconds,
                    hardSeconds: firstResponse.reviewTimeHardSeconds,
                    goodSeconds: firstResponse.reviewTimeGoodSeconds,
                    easySeconds: firstResponse.reviewTimeEasySeconds,
                    sampleCounts: firstResponse.reviewTimeSampleCounts,
                };
                reviewTimeAgainCoeffs = firstResponse.reviewTimeAgainCoeffs;
                reviewTimeHardCoeffs = firstResponse.reviewTimeHardCoeffs;
                reviewTimeGoodCoeffs = firstResponse.reviewTimeGoodCoeffs;
                reviewTimeEasyCoeffs = firstResponse.reviewTimeEasyCoeffs;
                reviewTimeGradeWeights = firstResponse.reviewTimeGradeWeights;
                reviewTimeTransitionProbs = firstResponse.reviewTimeTransitionProbs;
                reviewTimeTransitionCounts = firstResponse.reviewTimeTransitionCounts;
                reviewTimeSuccessGradeProbs = firstResponse.reviewTimeSuccessGradeProbs;
                reviewTimeSuccessGradeCounts =
                    firstResponse.reviewTimeSuccessGradeCounts;

                points = points.concat(
                    workloadPointsFromResponses(responses, runNumber),
                );

                tableData = [
                    ...renderWorkloadChart(
                        svg as SVGElement,
                        bounds,
                        points as WorkloadPoint[],
                        simulateWorkloadSubgraph,
                    ),
                    ...workloadSameMemorizedSavings(points as WorkloadPoint[]),
                ];
            }
        }
    }

    async function simulateRwkvWorkload(
        request: SimulateFsrsReviewRequest,
    ): Promise<SimulateFsrsWorkloadResponse> {
        try {
            await postProto("startRwkvWorkload", request, Empty, {
                alertOnError: false,
            });
            let response: SimulateFsrsWorkloadResponse | undefined = undefined;
            while (!response) {
                await delay(RWKV_WORKLOAD_RESULT_POLL_MS);
                response = await fetchRwkvWorkloadResult();
            }
            return response;
        } catch (err) {
            void postProto("cancelRwkvWorkload", new Empty({}), Empty, {
                alertOnError: false,
            }).catch(() => {});
            alert(err);
            throw err;
        }
    }

    function delay(ms: number): Promise<void> {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function fetchRwkvWorkloadResult(): Promise<
        SimulateFsrsWorkloadResponse | undefined
    > {
        const result = await fetch("/_anki/rwkvWorkloadResult", {
            method: "POST",
            headers: {
                "Content-Type": "application/binary",
            },
            body: new Uint8Array(),
        });
        if (result.status === 202) {
            return undefined;
        }
        if (!result.ok) {
            let msg = "something went wrong";
            try {
                msg = await result.text();
            } catch {
                // ignore
            }
            throw new Error(`${result.status}: ${msg}`);
        }
        return SimulateFsrsWorkloadResponse.fromBinary(
            new Uint8Array(await result.arrayBuffer()),
        );
    }

    function clearSimulation() {
        if (compareWorkloads) {
            fsrsComparisonPoints = fsrsComparisonPoints.filter(
                (point) => Math.floor(point.label / 1000) !== simulationNumber,
            );
            rwkvComparisonPoints = rwkvComparisonPoints.filter(
                (point) => Math.floor(point.label / 1000) !== simulationNumber,
            );
            simulationNumber = Math.max(0, simulationNumber - 1);
            return;
        }
        points = points.filter((p) =>
            workload
                ? Math.floor(p.label / 1000) !== simulationNumber
                : p.label !== simulationNumber,
        );
        simulationNumber = Math.max(0, simulationNumber - 1);
        reviewTimeMatrix = undefined;
        reviewTimeAgainCoeffs = [];
        reviewTimeHardCoeffs = [];
        reviewTimeGoodCoeffs = [];
        reviewTimeEasyCoeffs = [];
        reviewTimeGradeWeights = [];
        reviewTimeTransitionProbs = [];
        reviewTimeTransitionCounts = [];
        reviewTimeSuccessGradeProbs = [];
        reviewTimeSuccessGradeCounts = [];
        tableData = renderSimulationChart(
            svg as SVGElement,
            bounds,
            points,
            simulateSubgraph,
        );
    }

    function coeff(index: number, values: number[]): string {
        return (values[index] ?? 0).toFixed(4);
    }

    function gradeLabel(index: number): string {
        return ["Again", "Hard", "Good", "Easy"][index] ?? "";
    }

    function transitionIndex(fromGrade: number, toGrade: number): number {
        return fromGrade * 4 + toGrade;
    }

    function transitionProb(fromGrade: number, toGrade: number): number {
        return reviewTimeTransitionProbs[transitionIndex(fromGrade, toGrade)] ?? 0;
    }

    function transitionCount(fromGrade: number, toGrade: number): number {
        return reviewTimeTransitionCounts[transitionIndex(fromGrade, toGrade)] ?? 0;
    }

    function successGradeProb(rIndex: number, successGrade: number): number {
        return reviewTimeSuccessGradeProbs[rIndex * 3 + successGrade] ?? 0;
    }

    function successGradeCount(rIndex: number): number {
        return reviewTimeSuccessGradeCounts[rIndex] ?? 0;
    }

    function transitionSuccessPrior(successGrade: number): number {
        const again = Math.min(1, Math.max(0, reviewTimeGradeWeights[0] ?? 0.25));
        const successMass = Math.max(1e-6, 1 - again);
        const hard = (reviewTimeGradeWeights[1] ?? 0.25) / successMass;
        const good = (reviewTimeGradeWeights[2] ?? 0.25) / successMass;
        const easy = (reviewTimeGradeWeights[3] ?? 0.25) / successMass;
        const vals = [hard, good, easy];
        const sum = Math.max(1e-6, vals[0] + vals[1] + vals[2]);
        return vals[successGrade] / sum;
    }

    function blendedSuccessGradeProb(rIndex: number, successGrade: number): number {
        const alpha = Math.min(1, Math.max(0, transitionBlendAlpha));
        const scores = [
            Math.pow(Math.max(1e-6, successGradeProb(rIndex, 0) || 1 / 3), 1 - alpha) *
                Math.pow(Math.max(1e-6, transitionSuccessPrior(0) || 1 / 3), alpha),
            Math.pow(Math.max(1e-6, successGradeProb(rIndex, 1) || 1 / 3), 1 - alpha) *
                Math.pow(Math.max(1e-6, transitionSuccessPrior(1) || 1 / 3), alpha),
            Math.pow(Math.max(1e-6, successGradeProb(rIndex, 2) || 1 / 3), 1 - alpha) *
                Math.pow(Math.max(1e-6, transitionSuccessPrior(2) || 1 / 3), alpha),
        ];
        const sum = Math.max(1e-6, scores[0] + scores[1] + scores[2]);
        const normalized = [scores[0] / sum, scores[1] / sum, scores[2] / sum];
        return normalized[successGrade];
    }

    function finalGradeProb(rIndex: number, grade: number): number {
        const again = 1 - bucketRetrievability(rIndex);
        if (grade === 0) {
            return again;
        }
        const successMass = Math.max(0, 1 - again);
        return successMass * blendedSuccessGradeProb(rIndex, grade - 1);
    }

    function bucketRetrievability(rIndex: number): number {
        // midpoint of 5% bucket: [95,100] -> 0.975, [90,95) -> 0.925, ...
        return Math.max(0, Math.min(1, 1 - (rIndex * 0.05 + 0.025)));
    }

    function formatSeconds(seconds: number): string {
        return `${seconds.toFixed(1)}s`;
    }

    const graphWidth = 820;
    const graphHeight = 320;
    const graphMargin = { top: 18, right: 54, bottom: 46, left: 54 };
    const graphAgainColor = "#e5484d";
    const graphHardColor = "#f79009";
    const graphGoodColor = "#2bb24c";
    const graphEasyColor = "#6fd17e";
    const graphWeightedColor = "#ffffff";

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
        kind: "again" | "hard" | "good" | "easy" | "weighted";
        points: string;
    };
    let reviewTimeGraphLines: ReviewTimeGraphLine[] = [];
    let reviewTimeGraphTimeYMin = 0;
    let reviewTimeGraphTimeYMax = 1;
    let reviewTimeGraphXTicks: { rIndex: number; label: string }[] = [];
    let reviewTimeGraphTimeYTicks: number[] = [];

    $: if (reviewTimeMatrix) {
        const matrix = reviewTimeMatrix;
        reviewTimeSampleMedian = median(matrix.sampleCounts);
        const againLines = buildSLineSeries(
            matrix.againSeconds,
            matrix.rBucketCount,
            matrix.sBucketCount,
        );
        const hardLines = buildSLineSeries(
            matrix.hardSeconds,
            matrix.rBucketCount,
            matrix.sBucketCount,
        );
        const goodLines = buildSLineSeries(
            matrix.goodSeconds,
            matrix.rBucketCount,
            matrix.sBucketCount,
        );
        const easyLines = buildSLineSeries(
            matrix.easySeconds,
            matrix.rBucketCount,
            matrix.sBucketCount,
        );
        const [, againMax] = seriesMinMax(againLines);
        const [, hardMax] = seriesMinMax(hardLines);
        const [, goodMax] = seriesMinMax(goodLines);
        const [, easyMax] = seriesMinMax(easyLines);
        const weightedLines = againLines.map((line, sIndex) =>
            line.map((_value, rIndex) => {
                const again = againLines[sIndex][rIndex];
                const hard = hardLines[sIndex][rIndex];
                const good = goodLines[sIndex][rIndex];
                const easy = easyLines[sIndex][rIndex];
                const wAgain = finalGradeProb(rIndex, 0);
                const wHard = finalGradeProb(rIndex, 1);
                const wGood = finalGradeProb(rIndex, 2);
                const wEasy = finalGradeProb(rIndex, 3);
                return wAgain * again + wHard * hard + wGood * good + wEasy * easy;
            }),
        );
        const [, weightedMax] = seriesMinMax(weightedLines);
        reviewTimeGraphTimeYMin = 0;
        reviewTimeGraphTimeYMax = Math.max(
            60,
            againMax,
            hardMax,
            goodMax,
            easyMax,
            weightedMax,
        );

        const graphLines: ReviewTimeGraphLine[] = [];
        for (let sIndex = 0; sIndex < matrix.sBucketCount; sIndex++) {
            graphLines.push({
                color: graphAgainColor,
                kind: "again",
                points: linePoints(
                    againLines[sIndex],
                    matrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphHardColor,
                kind: "hard",
                points: linePoints(
                    hardLines[sIndex],
                    matrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphGoodColor,
                kind: "good",
                points: linePoints(
                    goodLines[sIndex],
                    matrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphEasyColor,
                kind: "easy",
                points: linePoints(
                    easyLines[sIndex],
                    matrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
            graphLines.push({
                color: graphWeightedColor,
                kind: "weighted",
                points: linePoints(
                    weightedLines[sIndex],
                    matrix.rBucketCount,
                    reviewTimeGraphTimeYMin,
                    reviewTimeGraphTimeYMax,
                ),
            });
        }
        reviewTimeGraphLines = graphLines;

        const xTickStep = Math.max(1, Math.floor(matrix.rBucketCount / 6));
        reviewTimeGraphXTicks = Array.from(
            { length: matrix.rBucketCount },
            (_, rIndex) => rIndex,
        )
            .filter(
                (rIndex) =>
                    rIndex % xTickStep === 0 || rIndex === matrix.rBucketCount - 1,
            )
            .map((rIndex) => ({ rIndex, label: rBucketLabel(rIndex) }));

        reviewTimeGraphTimeYTicks = Array.from({ length: 5 }, (_, i) => {
            const ratio = i / 4;
            return (
                reviewTimeGraphTimeYMin +
                ratio * (reviewTimeGraphTimeYMax - reviewTimeGraphTimeYMin)
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
            pointsToRender = smoothPointsByLabel(points, windowSize);
        }

        const render_function = workload ? renderWorkloadChart : renderSimulationChart;

        const chartTableData = render_function(
            svg as SVGElement,
            bounds,
            // This cast shouldn't matter because we aren't switching between modes in the same modal
            pointsToRender as WorkloadPoint[],
            (workload ? simulateWorkloadSubgraph : simulateSubgraph) as any as never,
        );
        tableData = workload
            ? [
                  ...chartTableData,
                  ...workloadSameMemorizedSavings(pointsToRender as WorkloadPoint[]),
              ]
            : chartTableData;
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
                    {#if compareWorkloads}
                        FSRS / RWKV Efficiency Comparison (Experimental)
                    {:else if rwkvWorkload}
                        RWKV Desired Retention Simulator (Experimental)
                    {:else if workload}
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
                <div class:comparison-controls={compareWorkloads}>
                    <SpinBoxRow
                        bind:value={daysToSimulate}
                        defaultValue={365}
                        min={1}
                        max={Infinity}
                    >
                        <SettingTitle
                            on:click={() => openHelpModal("simulateFsrsReview")}
                        >
                            {tr.deckConfigDaysToSimulate()}
                        </SettingTitle>
                    </SpinBoxRow>

                    {#if !rwkvWorkload}
                        <SpinBoxRow
                            bind:value={deckSize}
                            defaultValue={0}
                            min={0}
                            max={100000}
                        >
                            <SettingTitle
                                on:click={() => openHelpModal("simulateFsrsReview")}
                            >
                                {tr.deckConfigAdditionalNewCardsToSimulate()}
                            </SettingTitle>
                        </SpinBoxRow>
                    {/if}

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

                    {#if !rwkvWorkload || compareWorkloads}
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
                    {/if}

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

                    {#if rwkvWorkload && !compareWorkloads}
                        <SpinBoxRow
                            bind:value={rwkvWorkloadSampleLimit}
                            defaultValue={compareWorkloads
                                ? 0
                                : RWKV_WORKLOAD_SAMPLE_LIMIT_DEFAULT}
                            min={0}
                            max={100000}
                        >
                            <SettingTitle
                                on:click={() => openHelpModal("simulateFsrsReview")}
                            >
                                RWKV sample cap
                            </SettingTitle>
                        </SpinBoxRow>

                        <SpinBoxRow
                            bind:value={rwkvWorkloadTargetStep}
                            defaultValue={RWKV_WORKLOAD_TARGET_STEP_DEFAULT}
                            min={1}
                            max={70}
                        >
                            <SettingTitle
                                on:click={() => openHelpModal("simulateFsrsReview")}
                            >
                                RWKV DR step
                            </SettingTitle>
                        </SpinBoxRow>

                        <SpinBoxRow
                            bind:value={rwkvWorkloadStateUpdateInterval}
                            defaultValue={compareWorkloads
                                ? 1
                                : RWKV_WORKLOAD_STATE_UPDATE_INTERVAL_DEFAULT}
                            min={1}
                            max={1000}
                        >
                            <SettingTitle
                                on:click={() => openHelpModal("simulateFsrsReview")}
                            >
                                RWKV state stride
                            </SettingTitle>
                        </SpinBoxRow>
                    {/if}

                    {#if !rwkvWorkload || compareWorkloads}
                        {#if !compareWorkloads}
                            <details>
                                <summary>{tr.deckConfigEasyDaysTitle()}</summary>
                                {#key easyDayPercentages}
                                    <EasyDaysInput bind:values={easyDayPercentages} />
                                {/key}
                            </details>
                        {/if}

                        <details>
                            <summary>{tr.deckConfigAdvancedSettings()}</summary>
                            <SpinBoxRow
                                bind:value={simulateFsrsRequest.maxInterval}
                                defaultValue={$config.maximumReviewInterval}
                                min={1}
                                max={36500}
                            >
                                <SettingTitle
                                    on:click={() => openHelpModal("maximumInterval")}
                                >
                                    {tr.schedulingMaximumInterval()}
                                </SettingTitle>
                            </SpinBoxRow>

                            <EnumSelectorRow
                                bind:value={simulateFsrsRequest.reviewOrder}
                                defaultValue={$config.reviewOrder}
                                choices={reviewOrderChoices($fsrs)}
                            >
                                <SettingTitle
                                    on:click={() => openHelpModal("reviewSortOrder")}
                                >
                                    {tr.deckConfigReviewSortOrder()}
                                </SettingTitle>
                            </EnumSelectorRow>

                            <SwitchRow
                                bind:value={
                                    simulateFsrsRequest.newCardsIgnoreReviewLimit
                                }
                                defaultValue={$newCardsIgnoreReviewLimit}
                            >
                                <SettingTitle
                                    on:click={() =>
                                        openHelpModal("newCardsIgnoreReviewLimit")}
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

                            {#if !compareWorkloads}
                                <SwitchRow
                                    bind:value={simulateDynamicDesiredRetention}
                                    defaultValue={false}
                                    disabled={!dynamicDesiredRetentionAvailable}
                                >
                                    <SettingTitle
                                        on:click={() =>
                                            openHelpModal("simulateFsrsReview")}
                                    >
                                        Use Dynamic DR (ADR)
                                    </SettingTitle>
                                </SwitchRow>
                            {/if}

                            {#if workload && !compareWorkloads}
                                <SwitchRow
                                    bind:value={splitWorkloadByPreset}
                                    defaultValue={false}
                                >
                                    <SettingTitle
                                        on:click={() =>
                                            openHelpModal("simulateFsrsReview")}
                                    >
                                        {tr.deckConfigFsrsSimulatorSplitByPreset()}
                                    </SettingTitle>
                                </SwitchRow>

                                <SpinBoxFloatRow
                                    bind:value={transitionBlendAlpha}
                                    defaultValue={HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT}
                                    min={0}
                                    max={1}
                                >
                                    <SettingTitle
                                        on:click={() =>
                                            openHelpModal("simulateFsrsReview")}
                                    >
                                        Blend Alpha (R vs Prev Grade)
                                    </SettingTitle>
                                </SpinBoxFloatRow>

                                <SwitchRow
                                    bind:value={enforceMonotonicSuccessGradeProbs}
                                    defaultValue={HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT}
                                >
                                    <SettingTitle
                                        on:click={() =>
                                            openHelpModal("simulateFsrsReview")}
                                    >
                                        Enforce monotonic H/G/E by R
                                    </SettingTitle>
                                </SwitchRow>
                            {/if}

                            <SwitchRow
                                bind:value={suspendLeeches}
                                defaultValue={$config.leechAction ==
                                    DeckConfig_Config_LeechAction.SUSPEND}
                            >
                                <SettingTitle
                                    on:click={() => openHelpModal("leechAction")}
                                >
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
                    {/if}

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

                        {#if rwkvWorkload && simulating && workloadProgressString}
                            <div class="simulator-progress">
                                <div>{workloadProgressString}</div>
                                {#if workloadProgressPct !== undefined}
                                    <div
                                        class="progress"
                                        role="progressbar"
                                        aria-valuenow={workloadProgressPct}
                                        aria-valuemin="0"
                                        aria-valuemax="100"
                                    >
                                        <div
                                            class="progress-bar"
                                            style={`width: ${workloadProgressPct}%`}
                                        ></div>
                                    </div>
                                {/if}
                            </div>
                        {/if}
                    </div>
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
                                    {tr.deckConfigFsrsSimulatorRadioEfficiency()}
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
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.weightedMemorized}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioWeightedMemorized()}
                                </label>
                                <label>
                                    <input
                                        type="radio"
                                        value={SimulateWorkloadSubgraph.weightedRatio}
                                        bind:group={simulateWorkloadSubgraph}
                                    />
                                    {tr.deckConfigFsrsSimulatorRadioWeightedEfficiency()}
                                </label>
                            {/if}
                        </InputBox>
                    </div>

                    {#if compareWorkloads}
                        <SimulatorWorkloadGraph
                            points={comparisonRenderPoints}
                            subgraph={simulateWorkloadSubgraph}
                        />
                    {:else}
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
                    {/if}
                </Graph>

                {#if workload && reviewTimeMatrix && !compareWorkloads}
                    <details class="review-time-matrix mt-2">
                        <summary>
                            {tr.statisticsReviewsTimeCheckbox()} Matrix (R/S, Again/Hard/Good/Easy)
                        </summary>
                        <div class="review-time-matrix-wrapper">
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>R \\ S</th>
                                        {#each Array.from( { length: reviewTimeMatrix.sBucketCount }, ) as _, sIndex}
                                            <th>
                                                {sBucketLabel(
                                                    sIndex,
                                                    reviewTimeMatrix.sBucketCount,
                                                )}
                                            </th>
                                        {/each}
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each Array.from( { length: reviewTimeMatrix.rBucketCount }, ) as _, rIndex}
                                        <tr>
                                            <th>{rBucketLabel(rIndex)}</th>
                                            {#each Array.from( { length: reviewTimeMatrix.sBucketCount }, ) as _, sIndex}
                                                <td>
                                                    <div>
                                                        A {formatSeconds(
                                                            matrixCellValue(
                                                                reviewTimeMatrix.againSeconds,
                                                                rIndex,
                                                                sIndex,
                                                                reviewTimeMatrix.sBucketCount,
                                                            ),
                                                        )}
                                                    </div>
                                                    <div>
                                                        H {formatSeconds(
                                                            matrixCellValue(
                                                                reviewTimeMatrix.hardSeconds,
                                                                rIndex,
                                                                sIndex,
                                                                reviewTimeMatrix.sBucketCount,
                                                            ),
                                                        )}
                                                    </div>
                                                    <div>
                                                        G {formatSeconds(
                                                            matrixCellValue(
                                                                reviewTimeMatrix.goodSeconds,
                                                                rIndex,
                                                                sIndex,
                                                                reviewTimeMatrix.sBucketCount,
                                                            ),
                                                        )}
                                                    </div>
                                                    <div>
                                                        E {formatSeconds(
                                                            matrixCellValue(
                                                                reviewTimeMatrix.easySeconds,
                                                                rIndex,
                                                                sIndex,
                                                                reviewTimeMatrix.sBucketCount,
                                                            ),
                                                        )}
                                                    </div>
                                                    <div
                                                        class="review-time-samples {matrixCellValue(
                                                            reviewTimeMatrix.sampleCounts,
                                                            rIndex,
                                                            sIndex,
                                                            reviewTimeMatrix.sBucketCount,
                                                        ) > reviewTimeSampleMedian
                                                            ? 'high'
                                                            : 'low'}"
                                                    >
                                                        n {matrixCellValue(
                                                            reviewTimeMatrix.sampleCounts,
                                                            rIndex,
                                                            sIndex,
                                                            reviewTimeMatrix.sBucketCount,
                                                        )}
                                                    </div>
                                                </td>
                                            {/each}
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-matrix-wrapper mt-2">
                            <div class="p-2">
                                <code>
                                    time = a + b * (1 - R) + c * S + d * reps + e * D
                                </code>
                            </div>
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>Model</th>
                                        <th>a</th>
                                        <th>b</th>
                                        <th>c</th>
                                        <th>d</th>
                                        <th>e</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <th>Again</th>
                                        <td>{coeff(0, reviewTimeAgainCoeffs)}</td>
                                        <td>{coeff(1, reviewTimeAgainCoeffs)}</td>
                                        <td>{coeff(2, reviewTimeAgainCoeffs)}</td>
                                        <td>{coeff(3, reviewTimeAgainCoeffs)}</td>
                                        <td>{coeff(4, reviewTimeAgainCoeffs)}</td>
                                    </tr>
                                    <tr>
                                        <th>Hard</th>
                                        <td>{coeff(0, reviewTimeHardCoeffs)}</td>
                                        <td>{coeff(1, reviewTimeHardCoeffs)}</td>
                                        <td>{coeff(2, reviewTimeHardCoeffs)}</td>
                                        <td>{coeff(3, reviewTimeHardCoeffs)}</td>
                                        <td>{coeff(4, reviewTimeHardCoeffs)}</td>
                                    </tr>
                                    <tr>
                                        <th>Good</th>
                                        <td>{coeff(0, reviewTimeGoodCoeffs)}</td>
                                        <td>{coeff(1, reviewTimeGoodCoeffs)}</td>
                                        <td>{coeff(2, reviewTimeGoodCoeffs)}</td>
                                        <td>{coeff(3, reviewTimeGoodCoeffs)}</td>
                                        <td>{coeff(4, reviewTimeGoodCoeffs)}</td>
                                    </tr>
                                    <tr>
                                        <th>Easy</th>
                                        <td>{coeff(0, reviewTimeEasyCoeffs)}</td>
                                        <td>{coeff(1, reviewTimeEasyCoeffs)}</td>
                                        <td>{coeff(2, reviewTimeEasyCoeffs)}</td>
                                        <td>{coeff(3, reviewTimeEasyCoeffs)}</td>
                                        <td>{coeff(4, reviewTimeEasyCoeffs)}</td>
                                    </tr>
                                    <tr>
                                        <th>Weights</th>
                                        <td>
                                            A {(reviewTimeGradeWeights[0] ?? 0).toFixed(
                                                3,
                                            )}
                                        </td>
                                        <td>
                                            H {(reviewTimeGradeWeights[1] ?? 0).toFixed(
                                                3,
                                            )}
                                        </td>
                                        <td>
                                            G {(reviewTimeGradeWeights[2] ?? 0).toFixed(
                                                3,
                                            )}
                                        </td>
                                        <td>
                                            E {(reviewTimeGradeWeights[3] ?? 0).toFixed(
                                                3,
                                            )}
                                        </td>
                                        <td></td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-matrix-wrapper mt-2">
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>Prev \\ Next</th>
                                        {#each Array.from({ length: 4 }) as _, toGrade}
                                            <th>{gradeLabel(toGrade)}</th>
                                        {/each}
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each Array.from({ length: 4 }) as _, fromGrade}
                                        <tr>
                                            <th>{gradeLabel(fromGrade)}</th>
                                            {#each Array.from( { length: 4 }, ) as _, toGrade}
                                                <td>
                                                    <div>
                                                        {(
                                                            transitionProb(
                                                                fromGrade,
                                                                toGrade,
                                                            ) * 100
                                                        ).toFixed(1)}%
                                                    </div>
                                                    <div
                                                        class="review-time-samples {transitionCount(
                                                            fromGrade,
                                                            toGrade,
                                                        ) > 0
                                                            ? 'high'
                                                            : 'low'}"
                                                    >
                                                        n {transitionCount(
                                                            fromGrade,
                                                            toGrade,
                                                        )}
                                                    </div>
                                                </td>
                                            {/each}
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-matrix-wrapper mt-2">
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>R Bucket</th>
                                        <th>P(Hard | R)</th>
                                        <th>P(Good | R)</th>
                                        <th>P(Easy | R)</th>
                                        <th>n</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each Array.from( { length: reviewTimeMatrix.rBucketCount }, ) as _, rIndex}
                                        <tr>
                                            <th>{rBucketLabel(rIndex)}</th>
                                            <td>
                                                {(
                                                    successGradeProb(rIndex, 0) * 100
                                                ).toFixed(1)}%
                                            </td>
                                            <td>
                                                {(
                                                    successGradeProb(rIndex, 1) * 100
                                                ).toFixed(1)}%
                                            </td>
                                            <td>
                                                {(
                                                    successGradeProb(rIndex, 2) * 100
                                                ).toFixed(1)}%
                                            </td>
                                            <td
                                                class="review-time-samples {successGradeCount(
                                                    rIndex,
                                                ) > 0
                                                    ? 'high'
                                                    : 'low'}"
                                            >
                                                {successGradeCount(rIndex)}
                                            </td>
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-matrix-wrapper mt-2">
                            <table class="review-time-matrix-table">
                                <thead>
                                    <tr>
                                        <th>R Bucket</th>
                                        <th>Final P(Hard | success)</th>
                                        <th>Final P(Good | success)</th>
                                        <th>Final P(Easy | success)</th>
                                        <th>n</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each Array.from( { length: reviewTimeMatrix.rBucketCount }, ) as _, rIndex}
                                        <tr>
                                            <th>{rBucketLabel(rIndex)}</th>
                                            <td>
                                                {(
                                                    blendedSuccessGradeProb(rIndex, 0) *
                                                    100
                                                ).toFixed(1)}%
                                            </td>
                                            <td>
                                                {(
                                                    blendedSuccessGradeProb(rIndex, 1) *
                                                    100
                                                ).toFixed(1)}%
                                            </td>
                                            <td>
                                                {(
                                                    blendedSuccessGradeProb(rIndex, 2) *
                                                    100
                                                ).toFixed(1)}%
                                            </td>
                                            <td
                                                class="review-time-samples {successGradeCount(
                                                    rIndex,
                                                ) > 0
                                                    ? 'high'
                                                    : 'low'}"
                                            >
                                                {successGradeCount(rIndex)}
                                            </td>
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        </div>

                        <div class="review-time-graph-wrapper">
                            <svg viewBox={`0 0 ${graphWidth} ${graphHeight}`}>
                                {#each reviewTimeGraphTimeYTicks as tick}
                                    {@const y = graphY(
                                        tick,
                                        reviewTimeGraphTimeYMin,
                                        reviewTimeGraphTimeYMax,
                                    )}
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
                                        {y}
                                        text-anchor="end"
                                        dominant-baseline="middle"
                                        class="review-time-axis-label"
                                    >
                                        {tick.toFixed(1)}s
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
                                    {@const x = graphX(
                                        tick.rIndex,
                                        reviewTimeMatrix.rBucketCount,
                                    )}
                                    <line
                                        x1={x}
                                        x2={x}
                                        y1={graphHeight - graphMargin.bottom}
                                        y2={graphHeight - graphMargin.bottom + 4}
                                        stroke="currentColor"
                                        stroke-width="1"
                                    />
                                    <text
                                        {x}
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
                                        stroke-width="1.6"
                                        points={line.points}
                                    />
                                {/each}
                            </svg>
                        </div>

                        <div class="review-time-legend">
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line again"></span>
                                <span>Again time</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line hard"></span>
                                <span>Hard time</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line good"></span>
                                <span>Good time</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line easy"></span>
                                <span>Easy time</span>
                            </div>
                            <div class="review-time-legend-item">
                                <span class="review-time-legend-line weighted"></span>
                                <span>Weighted average time</span>
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
        --bs-modal-border-radius: 0;
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

    .comparison-controls {
        width: min(42rem, 100%);
        margin-inline: auto;
    }

    .btn {
        margin-bottom: 0.375rem;
    }

    .simulator-progress {
        width: min(24rem, 100%);
        margin-top: 0.25rem;
        font-size: 0.875rem;
    }

    .simulator-progress .progress {
        height: 0.5rem;
        margin-top: 0.35rem;
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

    .review-time-line.hard {
        stroke-dasharray: 6 4;
    }

    .review-time-line.easy {
        stroke-dasharray: 2.5 3;
    }

    .review-time-line.weighted {
        stroke-dasharray: 1 0;
        stroke-width: 2;
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

    .review-time-legend-line.again {
        border-top-color: #e5484d;
    }

    .review-time-legend-line.hard {
        border-top-color: #f79009;
        border-top-style: dashed;
    }

    .review-time-legend-line.good {
        border-top-color: #2bb24c;
    }

    .review-time-legend-line.easy {
        border-top-color: #6fd17e;
        border-top-style: dotted;
    }

    .review-time-legend-line.weighted {
        border-top-color: #ffffff;
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
