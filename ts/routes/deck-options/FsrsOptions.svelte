<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        ComputeRetentionProgress,
        ComputeParamsProgress_Phase,
        type ComputeParamsProgress,
    } from "@generated/anki/collection_pb";
    import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";
    import {
        computeFsrsParams,
        evaluateParams,
        evaluateParamsLegacy,
        getFsrsNewCardIntervals,
        getRetentionWorkload,
        setWantsAbort,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { runWithBackendProgress } from "@tslib/progress";

    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";

    import GlobalLabel from "./GlobalLabel.svelte";
    import {
        commitEditing,
        type DeckOptionsState,
        ValueTab,
        withSelectedFsrsParams,
    } from "./lib";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import Warning from "./Warning.svelte";
    import ParamsInputRow from "./ParamsInputRow.svelte";
    import ParamsSearchRow from "./ParamsSearchRow.svelte";
    import DynamicDesiredRetentionPlotModal from "./DynamicDesiredRetentionPlotModal.svelte";
    import SimulatorModal from "./SimulatorModal.svelte";
    import {
        deltaClass,
        formatDelta,
        formatMetric,
        formatPercentDelta,
        metricDelta,
        metricDeltaPercent,
    } from "./optimize-comparison";
    import {
        customDecayCandidates,
        formatDecay,
        supportsCustomDecayTable,
        withLastParam,
    } from "./custom-decay-table";
    import {
        readFsrs7SameDaySettings,
        withFsrs7SameDaySettings,
    } from "./fsrs-same-day-settings";
    import {
        readFsrsSearchSettings,
        withFsrsSearchSettings,
    } from "./fsrs-search-settings";
    import {
        costWeightForAverageDr,
        dynamicDesiredRetentionEnabled,
        schedulingTargetDr,
        targetDrCalibration,
        validCalibration,
        validPolicyParams,
        validRetentionBounds,
    } from "./dynamic-desired-retention";
    import {
        HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
        HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
    } from "./help-me-decide-defaults";
    import {
        DeckConfig_Config,
        DeckConfig_Config_FsrsVersion,
        GetRetentionWorkloadRequest,
        type GetRetentionWorkloadResponse,
        UpdateDeckConfigsMode,
    } from "@generated/anki/deck_config_pb";
    import type Modal from "bootstrap/js/dist/modal";
    import TabbedValue from "./TabbedValue.svelte";
    import Item from "$lib/components/Item.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";

    export let state: DeckOptionsState;
    export let openHelpModal: (String) => void;
    export let newlyEnabled = false;

    export function onPresetChange() {
        desiredRetentionTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.desiredRetention,
            (value) => ($config.desiredRetention = value!),
            $config.desiredRetention,
            null,
        );
        effectiveDesiredRetention =
            $limits.desiredRetention ?? $config.desiredRetention;
    }

    const config = state.currentConfig;
    const defaults = state.defaults;
    const fsrsReschedule = state.fsrsReschedule;
    const fsrsShortTermWithStepsEnabled = state.fsrsShortTermWithStepsEnabled;
    const fsrsLearningQueuesDisabled = state.fsrsLearningQueuesDisabled;
    const reviewFuzzEnabled = state.reviewFuzzEnabled;
    const reviewFuzzBase = state.reviewFuzzBase;
    const reviewFuzzFactorShort = state.reviewFuzzFactorShort;
    const reviewFuzzFactorMid = state.reviewFuzzFactorMid;
    const reviewFuzzFactorLong = state.reviewFuzzFactorLong;
    const auxData = state.currentAuxData;
    const daysSinceLastOptimization = state.daysSinceLastOptimization;
    const limits = state.deckLimits;

    $: lastOptimizationWarning =
        $daysSinceLastOptimization > 30 ? tr.deckConfigTimeToOptimize() : "";
    let desiredRetentionFocused = false;
    let desiredRetentionEverFocused = false;
    let optimized = false;
    const initialParams = [...selectedFsrsParams($config)];
    $: if (desiredRetentionFocused) {
        desiredRetentionEverFocused = true;
    }
    $: showDesiredRetentionTooltip =
        newlyEnabled || desiredRetentionEverFocused || optimized;

    let computeParamsProgress: ComputeParamsProgress | undefined;
    let computingParams = false;
    let checkingParams = false;
    let checkingHealth = false;
    type OptimizationMetrics = {
        logLoss: number;
        rmseBins: number;
    };
    type OptimizationComparison = {
        optimizedParams: number[];
        dynamicDesiredRetentionParams: number[];
        dynamicDesiredRetentionWeights: number[];
        dynamicDesiredRetentionAvgDrs: number[];
        dynamicDesiredRetentionFsrsEqWeights: number[];
        dynamicDesiredRetentionFsrsEqDrs: number[];
        dynamicDesiredRetentionMin: number;
        dynamicDesiredRetentionMax: number;
        search: string;
        ignoreRevlogsBeforeMs: bigint;
        current: OptimizationMetrics;
        optimized: OptimizationMetrics;
    };
    type DecayRow = {
        decay: number;
        isBaseline: boolean;
        logLoss: number;
        rmseBins: number;
        logLossDelta: number;
        logLossDeltaPercent: number | undefined;
        rmseDelta: number;
        rmseDeltaPercent: number | undefined;
    };
    type SameDayDecisionRow = {
        label: string;
        fsrsItems: number;
        allTargets: OptimizationMetrics;
        longTermTargets: OptimizationMetrics;
    };
    type SameDayParamsCandidate = {
        label: string;
        fsrsItems: number;
        params: number[];
    };
    type SameDayDecisionComparison = {
        withSameDay: SameDayDecisionRow;
        withoutSameDay: SameDayDecisionRow;
    };
    type SameDayRecommendation = {
        tone: "better" | "worse" | "equal";
        text: string;
    };
    let optimizationComparison: OptimizationComparison | undefined;
    let sameDayDecisionComparison: SameDayDecisionComparison | undefined;
    let customDecayRows: DecayRow[] = [];
    let loadingCustomDecayTable = false;
    let evaluationSearchFilter = "";
    let includeSameDayReviewsInFsrs7 = true;
    let checkingSameDayDecision = false;
    $: evaluationSearchFilter = readFsrsSearchSettings($auxData).evaluationSearch;
    $: {
        const updated = withFsrsSearchSettings($auxData, {
            evaluationSearch: evaluationSearchFilter,
        });
        if (updated) {
            auxData.set(updated);
        }
    }
    $: {
        const settings = readFsrs7SameDaySettings($auxData);
        includeSameDayReviewsInFsrs7 = settings.includeSameDayReviews;
    }
    $: {
        const updated = withFsrs7SameDaySettings($auxData, {
            includeSameDayReviews: includeSameDayReviewsInFsrs7,
        });
        if (updated) {
            auxData.set(updated);
        }
    }
    const fsrsVersionChoices = [
        {
            value: DeckConfig_Config_FsrsVersion.SEVEN,
            label: "FSRS-7",
        },
        {
            value: DeckConfig_Config_FsrsVersion.SIX,
            label: "FSRS-6",
        },
        {
            value: DeckConfig_Config_FsrsVersion.FIVE,
            label: "FSRS-5",
        },
        {
            value: DeckConfig_Config_FsrsVersion.FOUR,
            label: "FSRS-4.5",
        },
    ];

    function selectedFsrsParams(config: DeckConfig_Config): number[] {
        switch (config.fsrsVersion) {
            case DeckConfig_Config_FsrsVersion.SIX:
                return config.fsrsParams6;
            case DeckConfig_Config_FsrsVersion.FIVE:
                return config.fsrsParams5;
            case DeckConfig_Config_FsrsVersion.FOUR:
                return config.fsrsParams4;
            default:
                return config.fsrsParams7;
        }
    }

    function setSelectedFsrsParams(params: number[]): void {
        config.update((current) => withSelectedFsrsParams(current, params));
    }

    const healthCheck = state.fsrsHealthCheck;

    $: computing =
        computingParams || checkingParams || checkingHealth || checkingSameDayDecision;
    $: defaultparamSearch = `preset:"${state.getCurrentNameForSearch()}" -is:suspended`;
    $: roundedRetention = Number(effectiveDesiredRetention.toFixed(2));
    $: desiredRetentionWarning = getRetentionLongShortWarning(roundedRetention);

    let desiredRetentionChangeInfo = "";
    $: if (showDesiredRetentionTooltip) {
        getRetentionChangeInfo(roundedRetention, selectedFsrsParams($config));
    }

    $: retentionWarningClass = getRetentionWarningClass(roundedRetention);

    $: newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;

    // Create tabs for desired retention
    const desiredRetentionTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.desiredRetention,
            (value) => ($config.desiredRetention = value!),
            $config.desiredRetention,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.desiredRetention ?? null,
            (value) => ($limits.desiredRetention = value ?? undefined),
            null,
            null,
        ),
    ];

    // Get the effective desired retention value (deck-specific if set, otherwise config default)
    let effectiveDesiredRetention =
        $limits.desiredRetention ?? $config.desiredRetention;
    const startingDesiredRetention = effectiveDesiredRetention.toFixed(2);
    const startingDesiredRetentionValue = Number(startingDesiredRetention);
    const intervalColumns = [
        tr.studyingAgain(),
        tr.studyingHard(),
        tr.studyingGood(),
        tr.studyingEasy(),
        tr.deckConfigAgainThenGood(),
        tr.deckConfigAgainThenAgain(),
        tr.deckConfigGoodThenAgain(),
        tr.deckConfigGoodThenGood(),
    ];
    const intervalRowClasses = [
        "interval-again",
        "interval-hard",
        "interval-good",
        "interval-easy",
        "",
        "",
        "",
        "",
    ];
    let newCardIntervals: [string[], string[]] | undefined;
    let newCardIntervalsError = "";
    let newCardIntervalRequest = 0;

    $: simulateFsrsRequest = new SimulateFsrsReviewRequest({
        params: selectedFsrsParams($config),
        desiredRetention: $config.desiredRetention,
        newLimit: $config.newPerDay,
        reviewLimit: $config.reviewsPerDay,
        maxInterval: $config.maximumReviewInterval,
        search: `preset:"${state.getCurrentNameForSearch()}" -is:suspended`,
        newCardsIgnoreReviewLimit: $newCardsIgnoreReviewLimit,
        easyDaysPercentages: $config.easyDaysPercentages,
        reviewOrder: $config.reviewOrder,
        historicalRetention: $config.historicalRetention,
        learningStepCount: $config.learnSteps.length,
        relearningStepCount: $config.relearnSteps.length,
        reviewFuzzBase: $reviewFuzzEnabled ? $reviewFuzzBase : 0,
        reviewFuzzFactorShort: $reviewFuzzEnabled ? $reviewFuzzFactorShort : 0,
        reviewFuzzFactorMid: $reviewFuzzEnabled ? $reviewFuzzFactorMid : 0,
        reviewFuzzFactorLong: $reviewFuzzEnabled ? $reviewFuzzFactorLong : 0,
        helpMeDecideTransitionBlendAlpha: HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
        helpMeDecideEnforceMonotonicSuccessGradeProbs:
            HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    });

    $: void loadNewCardIntervals(
        startingDesiredRetentionValue,
        effectiveDesiredRetention,
        $fsrsShortTermWithStepsEnabled,
        $fsrsLearningQueuesDisabled,
        selectedFsrsParams($config),
        $config.learnSteps,
        $config.relearnSteps,
        $config.maximumReviewInterval,
        $config.fsrsMinimumIntervalSecs,
        $config.graduatingIntervalGood,
        $config.graduatingIntervalEasy,
        $config.initialEase,
        $config.hardMultiplier,
        $config.easyMultiplier,
        $config.intervalMultiplier,
        $config.leechThreshold,
        $config.lapseMultiplier,
        $config.minimumLapseInterval,
    );

    const DESIRED_RETENTION_LOW_THRESHOLD = 0.8;
    const DESIRED_RETENTION_HIGH_THRESHOLD = 0.95;

    function getRetentionLongShortWarning(retention: number) {
        if (retention < DESIRED_RETENTION_LOW_THRESHOLD) {
            return tr.deckConfigDesiredRetentionTooLow();
        } else if (retention > DESIRED_RETENTION_HIGH_THRESHOLD) {
            return tr.deckConfigDesiredRetentionTooHigh();
        } else {
            return "";
        }
    }

    let retentionWorkloadInfo: undefined | Promise<GetRetentionWorkloadResponse> =
        undefined;
    let lastParams = [...selectedFsrsParams($config)];

    function configWithDesiredRetention(
        currentConfig: DeckConfig_Config,
        desiredRetention: number,
    ): DeckConfig_Config {
        const config = new DeckConfig_Config(currentConfig);
        config.desiredRetention = desiredRetention;
        return config;
    }

    async function loadNewCardIntervals(
        currentRetention: number,
        selectedRetention: number,
        fsrsShortTermWithStepsEnabled: boolean,
        fsrsLearningQueuesDisabled: boolean,
        params: number[],
        _learnSteps: number[],
        _relearnSteps: number[],
        _maximumReviewInterval: number,
        _fsrsMinimumIntervalSecs: number,
        _graduatingIntervalGood: number,
        _graduatingIntervalEasy: number,
        _initialEase: number,
        _hardMultiplier: number,
        _easyMultiplier: number,
        _intervalMultiplier: number,
        _leechThreshold: number,
        _lapseMultiplier: number,
        _minimumLapseInterval: number,
    ): Promise<void> {
        const request = ++newCardIntervalRequest;
        newCardIntervalsError = "";
        const currentConfig = withSelectedFsrsParams($config, params);
        try {
            const [current, selected] = await Promise.all([
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(currentConfig, currentRetention),
                    fsrsShortTermWithStepsEnabled,
                    fsrsLearningQueuesDisabled,
                }),
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(
                        currentConfig,
                        selectedRetention,
                    ),
                    fsrsShortTermWithStepsEnabled,
                    fsrsLearningQueuesDisabled,
                }),
            ]);
            if (request !== newCardIntervalRequest) {
                return;
            }
            newCardIntervals = [current.vals, selected.vals];
            newCardIntervalsError = "";
        } catch (err) {
            if (request === newCardIntervalRequest) {
                newCardIntervals = undefined;
                newCardIntervalsError =
                    err instanceof Error ? err.message : String(err);
                console.error("failed to load FSRS new-card intervals", err);
            }
        }
    }

    async function getRetentionChangeInfo(retention: number, params: number[]) {
        if (+startingDesiredRetention == roundedRetention) {
            desiredRetentionChangeInfo = tr.deckConfigWorkloadFactorUnchanged();
            return;
        }
        if (
            // If the cache is empty and a request has not yet been made to fill it
            !retentionWorkloadInfo ||
            // If the parameters have been changed
            lastParams.toString() !== params.toString()
        ) {
            const request = new GetRetentionWorkloadRequest({
                w: params,
                search: defaultparamSearch,
            });
            lastParams = [...params];
            retentionWorkloadInfo = getRetentionWorkload(request);
        }

        const previous = +startingDesiredRetention * 100;
        const after = retention * 100;
        const resp = await retentionWorkloadInfo;
        const factor = resp.costs[after] / resp.costs[previous];

        desiredRetentionChangeInfo = tr.deckConfigWorkloadFactorChange({
            factor: factor.toFixed(2),
            previousDr: previous.toString(),
        });
    }

    function getRetentionWarningClass(retention: number): string {
        if (retention < 0.7 || retention > 0.97) {
            return "alert-danger";
        } else if (
            retention < DESIRED_RETENTION_LOW_THRESHOLD ||
            retention > DESIRED_RETENTION_HIGH_THRESHOLD
        ) {
            return "alert-warning";
        } else {
            return "alert-info";
        }
    }

    function getIgnoreRevlogsBeforeMs() {
        return BigInt(
            $config.ignoreRevlogsBeforeDate
                ? new Date($config.ignoreRevlogsBeforeDate).getTime()
                : 0,
        );
    }

    function getNumOfRelearningStepsInDay(): number {
        const relearningSteps = $config.relearnSteps;
        let numOfRelearningStepsInDay = 0;
        let accumulatedTime = 0;
        for (let i = 0; i < relearningSteps.length; i++) {
            accumulatedTime += relearningSteps[i];
            if (accumulatedTime >= 1440) {
                break;
            }
            numOfRelearningStepsInDay++;
        }
        return numOfRelearningStepsInDay;
    }

    function optimizeSearchFilter(): string {
        return $config.paramSearch ? $config.paramSearch : defaultparamSearch;
    }

    function evaluateSearchFilter(): string {
        return evaluationSearchFilter.trim()
            ? evaluationSearchFilter
            : optimizeSearchFilter();
    }

    function includeSameDayOverride(): boolean | undefined {
        if ($config.fsrsVersion !== DeckConfig_Config_FsrsVersion.SEVEN) {
            return undefined;
        }
        return includeSameDayReviewsInFsrs7;
    }

    async function computeParams(): Promise<void> {
        if (computingParams) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        await commitEditing();
        computingParams = true;
        computeParamsProgress = undefined;
        try {
            await runWithBackendProgress(
                async () => {
                    const params = selectedFsrsParams($config);
                    const search = optimizeSearchFilter();
                    const evaluateSearch = evaluateSearchFilter();
                    const resp = await computeFsrsParams({
                        search,
                        ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                        currentParams: params,
                        numOfRelearningSteps: getNumOfRelearningStepsInDay(),
                        healthCheck: $healthCheck,
                        includeSameDayReviews: includeSameDayOverride(),
                        fsrsVersion: $config.fsrsVersion,
                        dynamicDesiredRetentionEnabled:
                            $config.fsrsVersion ===
                                DeckConfig_Config_FsrsVersion.SEVEN &&
                            $config.fsrsDynamicDesiredRetentionEnabled,
                    });

                    const alreadyOptimal =
                        (params.length &&
                            params.every(
                                (n, i) => n.toFixed(4) === resp.params[i].toFixed(4),
                            )) ||
                        resp.params.length === 0;

                    let healthCheckMessage = "";
                    if (resp.healthCheckPassed !== undefined) {
                        healthCheckMessage = resp.healthCheckPassed
                            ? tr.deckConfigFsrsGoodFit()
                            : "";
                    }
                    let alreadyOptimalMessage = "";
                    if (alreadyOptimal) {
                        alreadyOptimalMessage = resp.fsrsItems
                            ? tr.deckConfigFsrsParamsOptimal()
                            : tr.deckConfigFsrsParamsNoReviews();
                    }
                    const message = [alreadyOptimalMessage, healthCheckMessage]
                        .filter((a) => a)
                        .join("\n\n");

                    if (message) {
                        setTimeout(() => alert(message), 200);
                    }

                    const dynamicDesiredRetentionParams = [
                        ...resp.fsrsDynamicDesiredRetentionParams,
                    ];
                    const dynamicDesiredRetentionWeights = [
                        ...resp.fsrsDynamicDesiredRetentionWeights,
                    ];
                    const dynamicDesiredRetentionAvgDrs = [
                        ...resp.fsrsDynamicDesiredRetentionAvgDrs,
                    ];
                    const dynamicDesiredRetentionFsrsEqWeights = [
                        ...resp.fsrsDynamicDesiredRetentionFsrsEqWeights,
                    ];
                    const dynamicDesiredRetentionFsrsEqDrs = [
                        ...resp.fsrsDynamicDesiredRetentionFsrsEqDrs,
                    ];
                    const dynamicDesiredRetentionMin =
                        resp.fsrsDynamicDesiredRetentionMin;
                    const dynamicDesiredRetentionMax =
                        resp.fsrsDynamicDesiredRetentionMax;
                    if (alreadyOptimal && dynamicDesiredRetentionParams.length) {
                        $config.fsrsDynamicDesiredRetentionParams =
                            dynamicDesiredRetentionParams;
                        $config.fsrsDynamicDesiredRetentionWeights =
                            dynamicDesiredRetentionWeights;
                        $config.fsrsDynamicDesiredRetentionAvgDrs =
                            dynamicDesiredRetentionAvgDrs;
                        $config.fsrsDynamicDesiredRetentionFsrsEqWeights =
                            dynamicDesiredRetentionFsrsEqWeights;
                        $config.fsrsDynamicDesiredRetentionFsrsEqDrs =
                            dynamicDesiredRetentionFsrsEqDrs;
                        $config.fsrsDynamicDesiredRetentionMin =
                            dynamicDesiredRetentionMin;
                        $config.fsrsDynamicDesiredRetentionMax =
                            dynamicDesiredRetentionMax;
                    }

                    if (!alreadyOptimal) {
                        const currentMetrics = await evaluateParamsLegacy({
                            search: evaluateSearch,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            params,
                            includeSameDayReviews: includeSameDayOverride(),
                        });
                        const optimizedMetrics = await evaluateParamsLegacy({
                            search: evaluateSearch,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            params: resp.params,
                            includeSameDayReviews: includeSameDayOverride(),
                        });
                        optimizationComparison = {
                            optimizedParams: [...resp.params],
                            dynamicDesiredRetentionParams,
                            dynamicDesiredRetentionWeights,
                            dynamicDesiredRetentionAvgDrs,
                            dynamicDesiredRetentionFsrsEqWeights,
                            dynamicDesiredRetentionFsrsEqDrs,
                            dynamicDesiredRetentionMin,
                            dynamicDesiredRetentionMax,
                            search: evaluateSearch,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            current: {
                                logLoss: currentMetrics.logLoss,
                                rmseBins: currentMetrics.rmseBins,
                            },
                            optimized: {
                                logLoss: optimizedMetrics.logLoss,
                                rmseBins: optimizedMetrics.rmseBins,
                            },
                        };
                    }
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } finally {
            computingParams = false;
        }
    }

    function closeOptimizationComparison(): void {
        optimizationComparison = undefined;
        customDecayRows = [];
        loadingCustomDecayTable = false;
    }

    function keepCurrentParams(): void {
        closeOptimizationComparison();
    }

    function applyOptimizedParams(): void {
        if (!optimizationComparison) {
            return;
        }
        setSelectedFsrsParams(optimizationComparison.optimizedParams);
        if (optimizationComparison.dynamicDesiredRetentionParams.length) {
            $config.fsrsDynamicDesiredRetentionParams =
                optimizationComparison.dynamicDesiredRetentionParams;
            $config.fsrsDynamicDesiredRetentionWeights =
                optimizationComparison.dynamicDesiredRetentionWeights;
            $config.fsrsDynamicDesiredRetentionAvgDrs =
                optimizationComparison.dynamicDesiredRetentionAvgDrs;
            $config.fsrsDynamicDesiredRetentionFsrsEqWeights =
                optimizationComparison.dynamicDesiredRetentionFsrsEqWeights;
            $config.fsrsDynamicDesiredRetentionFsrsEqDrs =
                optimizationComparison.dynamicDesiredRetentionFsrsEqDrs;
            $config.fsrsDynamicDesiredRetentionMin =
                optimizationComparison.dynamicDesiredRetentionMin;
            $config.fsrsDynamicDesiredRetentionMax =
                optimizationComparison.dynamicDesiredRetentionMax;
        }
        optimized = true;
        closeOptimizationComparison();
    }

    async function loadCustomDecayTable(): Promise<void> {
        if (
            !optimizationComparison ||
            loadingCustomDecayTable ||
            !supportsCustomDecayTable(optimizationComparison.optimizedParams)
        ) {
            return;
        }
        loadingCustomDecayTable = true;
        try {
            const comparison = optimizationComparison;
            const rows = await Promise.all(
                customDecayCandidates.map(async (decay) => {
                    const resp = await evaluateParamsLegacy({
                        search: comparison.search,
                        ignoreRevlogsBeforeMs: comparison.ignoreRevlogsBeforeMs,
                        params: withLastParam(comparison.optimizedParams, decay),
                        includeSameDayReviews: includeSameDayOverride(),
                    });
                    return {
                        decay,
                        isBaseline: false,
                        logLoss: resp.logLoss,
                        rmseBins: resp.rmseBins,
                        logLossDelta: metricDelta(
                            comparison.optimized.logLoss,
                            resp.logLoss,
                        ),
                        logLossDeltaPercent: metricDeltaPercent(
                            comparison.optimized.logLoss,
                            resp.logLoss,
                        ),
                        rmseDelta: metricDelta(
                            comparison.optimized.rmseBins,
                            resp.rmseBins,
                        ),
                        rmseDeltaPercent: metricDeltaPercent(
                            comparison.optimized.rmseBins,
                            resp.rmseBins,
                        ),
                    };
                }),
            );
            const optimizedDecay =
                comparison.optimizedParams[comparison.optimizedParams.length - 1] ?? 0;
            customDecayRows = [
                {
                    decay: optimizedDecay,
                    isBaseline: true,
                    logLoss: optimizationComparison.optimized.logLoss,
                    rmseBins: optimizationComparison.optimized.rmseBins,
                    logLossDelta: 0,
                    logLossDeltaPercent: 0,
                    rmseDelta: 0,
                    rmseDeltaPercent: 0,
                },
                ...rows,
            ];
        } finally {
            loadingCustomDecayTable = false;
        }
    }

    async function sameDayParamsCandidate(
        label: string,
        includeSameDayReviews: boolean,
    ): Promise<SameDayParamsCandidate> {
        const paramsResp = await computeFsrsParams({
            search: optimizeSearchFilter(),
            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
            currentParams: selectedFsrsParams($config),
            numOfRelearningSteps: getNumOfRelearningStepsInDay(),
            healthCheck: false,
            includeSameDayReviews,
            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
            dynamicDesiredRetentionEnabled: false,
        });
        return {
            label,
            fsrsItems: paramsResp.fsrsItems,
            params: paramsResp.params,
        };
    }

    async function sameDayDecisionRow(
        candidate: SameDayParamsCandidate,
    ): Promise<SameDayDecisionRow> {
        const search = evaluateSearchFilter();
        const allTargets = await evaluateParamsLegacy({
            search,
            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
            params: candidate.params,
            includeSameDayReviews: true,
        });
        const longTermTargets = await evaluateParamsLegacy({
            search,
            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
            params: candidate.params,
            includeSameDayReviews: false,
        });
        return {
            label: candidate.label,
            fsrsItems: candidate.fsrsItems,
            allTargets: {
                logLoss: allTargets.logLoss,
                rmseBins: allTargets.rmseBins,
            },
            longTermTargets: {
                logLoss: longTermTargets.logLoss,
                rmseBins: longTermTargets.rmseBins,
            },
        };
    }

    async function checkSameDayDecision(): Promise<void> {
        if (checkingSameDayDecision) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        await commitEditing();
        checkingSameDayDecision = true;
        computeParamsProgress = undefined;
        sameDayDecisionComparison = undefined;
        try {
            await runWithBackendProgress(
                async () => {
                    const withSameDayParams = await sameDayParamsCandidate(
                        "Optimized with same-day reviews",
                        true,
                    );
                    const withoutSameDayParams = await sameDayParamsCandidate(
                        "Optimized without same-day reviews",
                        false,
                    );
                    if (
                        !withSameDayParams.fsrsItems ||
                        !withoutSameDayParams.fsrsItems
                    ) {
                        alert(
                            "Not enough review history to compare same-day settings.",
                        );
                        return;
                    }
                    const withSameDay = await sameDayDecisionRow(withSameDayParams);
                    const withoutSameDay =
                        await sameDayDecisionRow(withoutSameDayParams);
                    sameDayDecisionComparison = { withSameDay, withoutSameDay };
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } finally {
            checkingSameDayDecision = false;
        }
    }

    function sameDayMetricClass(value: number, otherValue: number): string {
        return `optimize-delta ${deltaClass(metricDelta(otherValue, value))}`;
    }

    function sameDayRecommendation(
        comparison: SameDayDecisionComparison,
    ): SameDayRecommendation {
        const allLogLossDelta = metricDelta(
            comparison.withoutSameDay.allTargets.logLoss,
            comparison.withSameDay.allTargets.logLoss,
        );
        const longTermLogLossDelta = metricDelta(
            comparison.withoutSameDay.longTermTargets.logLoss,
            comparison.withSameDay.longTermTargets.logLoss,
        );
        const allLogLossDeltaText = formatDelta(allLogLossDelta);
        const longTermLogLossDeltaText = formatDelta(longTermLogLossDelta);

        if (allLogLossDelta === 0 && longTermLogLossDelta === 0) {
            return {
                tone: "equal",
                text: "No clear winner: both options have the same log loss on all targets and long-term-only targets.",
            };
        }

        if (
            longTermLogLossDelta <= 0 ||
            (allLogLossDelta < 0 && Math.abs(allLogLossDelta) >= longTermLogLossDelta)
        ) {
            return {
                tone: "better",
                text:
                    `Recommended: include same-day reviews. ` +
                    `All-target log loss changes by ${allLogLossDeltaText}, ` +
                    `and long-term-only log loss changes by ${longTermLogLossDeltaText}.`,
            };
        }

        return {
            tone: "worse",
            text:
                `Recommended: exclude same-day reviews. ` +
                `All-target log loss changes by ${allLogLossDeltaText}, ` +
                `but long-term-only log loss changes by ${longTermLogLossDeltaText}.`,
        };
    }

    async function checkParams(): Promise<void> {
        if (checkingParams) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        checkingParams = true;
        computeParamsProgress = undefined;
        try {
            await runWithBackendProgress(
                async () => {
                    const search = evaluateSearchFilter();
                    const resp = await evaluateParamsLegacy({
                        search,
                        ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                        params: selectedFsrsParams($config),
                        includeSameDayReviews: includeSameDayOverride(),
                    });
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                    setTimeout(
                        () =>
                            alert(
                                `Log loss: ${resp.logLoss.toFixed(4)}, RMSE(bins): ${(
                                    resp.rmseBins * 100
                                ).toFixed(2)}%. ${tr.deckConfigSmallerIsBetter()}`,
                            ),
                        200,
                    );
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } finally {
            checkingParams = false;
        }
    }

    async function checkHealth(): Promise<void> {
        if (checkingHealth) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        checkingHealth = true;
        computeParamsProgress = undefined;
        try {
            await runWithBackendProgress(
                async () => {
                    const search = evaluateSearchFilter();
                    const searchForTraining = optimizeSearchFilter();
                    const resp = await evaluateParams({
                        search,
                        searchForTraining,
                        ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                        numOfRelearningSteps: getNumOfRelearningStepsInDay(),
                        fsrsVersion: $config.fsrsVersion,
                        includeSameDayReviews: includeSameDayOverride(),
                        includeSameDayReviewsForTraining: includeSameDayOverride(),
                    });
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                    setTimeout(
                        () =>
                            alert(
                                `Log loss: ${resp.logLoss.toFixed(4)}, RMSE(bins): ${(
                                    resp.rmseBins * 100
                                ).toFixed(2)}%. ${tr.deckConfigSmallerIsBetter()}`,
                            ),
                        200,
                    );
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } finally {
            checkingHealth = false;
        }
    }

    $: computeParamsProgressString = renderWeightProgress(computeParamsProgress);
    $: computeParamsProgressPct = renderComputeParamsProgressPct(computeParamsProgress);
    $: totalReviews = computeParamsProgress?.reviews ?? undefined;

    function renderComputeParamsProgressPct(
        val: ComputeParamsProgress | undefined,
    ): number | undefined {
        if (!val || !val.total) {
            return undefined;
        }
        return Math.min(100, Math.max(0, (val.current / val.total) * 100));
    }

    function renderWeightProgress(val: ComputeParamsProgress | undefined): String {
        const pctValue = renderComputeParamsProgressPct(val);
        if (!val || pctValue === undefined) {
            return "";
        }
        const pct = pctValue.toFixed(1);
        if (val instanceof ComputeRetentionProgress) {
            return `${pct}%`;
        } else if (
            val.phase === ComputeParamsProgress_Phase.TRAINING_DYNAMIC_DESIRED_RETENTION
        ) {
            return `Compute ADR values (SSP-MMC): ${pct}%`;
        } else {
            if (val.current === val.total) {
                return tr.deckConfigCheckingForImprovement();
            } else {
                return tr.deckConfigPercentOfReviews({ pct, reviews: val.reviews });
            }
        }
    }

    async function computeAllParams(): Promise<void> {
        await commitEditing();
        state.save(UpdateDeckConfigsMode.COMPUTE_ALL_PARAMS);
    }

    function showSimulatorModal(modal: Modal) {
        if (selectedFsrsParams($config).toString() === initialParams.toString()) {
            modal?.show();
        } else {
            alert(tr.deckConfigFsrsSimulateSavePreset());
        }
    }

    let simulatorModal: Modal;
    let workloadModal: Modal;
    let dynamicDesiredRetentionPlotModal: Modal;
    const dynamicDesiredRetentionCalibrationCounts = Array.from(
        { length: 31 },
        (_, index) => index + 2,
    );
    dynamicDesiredRetentionCalibrationCounts.unshift(0);
    $: dynamicDesiredRetentionTargetCalibration = targetDrCalibration(
        $config.fsrsDynamicDesiredRetentionWeights,
        $config.fsrsDynamicDesiredRetentionAvgDrs,
        $config.fsrsDynamicDesiredRetentionFsrsEqWeights,
        $config.fsrsDynamicDesiredRetentionFsrsEqDrs,
    );
    $: dynamicDesiredRetentionSchedulingTarget = schedulingTargetDr(
        effectiveDesiredRetention,
        dynamicDesiredRetentionTargetCalibration.weights,
        dynamicDesiredRetentionTargetCalibration.drs,
        $config.fsrsDynamicDesiredRetentionClamp,
    );
    $: dynamicDesiredRetentionWeight = costWeightForAverageDr(
        dynamicDesiredRetentionSchedulingTarget,
        dynamicDesiredRetentionTargetCalibration.weights,
        dynamicDesiredRetentionTargetCalibration.drs,
    );
    $: dynamicDesiredRetentionConfigReady = dynamicDesiredRetentionEnabled($config);
    $: dynamicDesiredRetentionReady =
        dynamicDesiredRetentionConfigReady && dynamicDesiredRetentionWeight !== null;
    $: dynamicDesiredRetentionWarning = dynamicDesiredRetentionWarningMessage(
        $config,
        dynamicDesiredRetentionConfigReady,
        dynamicDesiredRetentionWeight,
    );

    function dynamicDesiredRetentionWarningMessage(
        config: DeckConfig_Config,
        configReady: boolean,
        weight: number | null,
    ): string {
        if (!config.fsrsDynamicDesiredRetentionEnabled) {
            return "";
        }
        if (
            !validPolicyParams(config.fsrsDynamicDesiredRetentionParams) ||
            !validCalibration(
                config.fsrsDynamicDesiredRetentionWeights,
                config.fsrsDynamicDesiredRetentionAvgDrs,
            )
        ) {
            return "Dynamic DR requires 15 SSP-MMC parameters and matching calibration arrays.";
        }
        if (
            !validRetentionBounds(
                config.fsrsDynamicDesiredRetentionMin,
                config.fsrsDynamicDesiredRetentionMax,
            )
        ) {
            return "Dynamic DR requires valid retention bounds.";
        }
        if (configReady && weight === null) {
            return "Dynamic DR target is outside the calibrated target range.";
        }
        return "";
    }

    function formatDynamicDrBound(value: number): string {
        return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "n/a";
    }

    function saveDynamicDesiredRetentionPlotTarget(event: CustomEvent<number>): void {
        effectiveDesiredRetention = event.detail;
        if ($limits.desiredRetention !== undefined) {
            desiredRetentionTabs[1].setValue(event.detail);
        } else {
            desiredRetentionTabs[0].setValue(event.detail);
        }
    }
</script>

<DynamicallySlottable slotHost={Item} api={{}}>
    <Item>
        <SpinBoxFloatRow
            bind:value={effectiveDesiredRetention}
            defaultValue={defaults.desiredRetention}
            min={0.1}
            max={0.99}
            percentage={true}
            bind:focused={desiredRetentionFocused}
        >
            <TabbedValue
                slot="tabs"
                tabs={desiredRetentionTabs}
                bind:value={effectiveDesiredRetention}
            />
            <SettingTitle on:click={() => openHelpModal("desiredRetention")}>
                {tr.deckConfigDesiredRetention()}
            </SettingTitle>
        </SpinBoxFloatRow>
    </Item>
</DynamicallySlottable>
<Warning warning={desiredRetentionChangeInfo} className={"alert-info two-line"} />
<Warning warning={desiredRetentionWarning} className={retentionWarningClass} />

{#if newCardIntervals}
    <div class="interval-preview ms-1 me-1">
        <div class="interval-preview-title">
            {tr.deckConfigNewCardIntervals()}
        </div>
        <table class="interval-preview-table">
            <thead>
                <tr>
                    <th></th>
                    <th>
                        {tr.deckConfigCurrentDr()}
                        ({(startingDesiredRetentionValue * 100).toFixed(2)}%)
                    </th>
                    <th>
                        {tr.deckConfigSelectedDr()}
                        ({(effectiveDesiredRetention * 100).toFixed(2)}%)
                    </th>
                </tr>
            </thead>
            <tbody>
                {#each intervalColumns as column, index}
                    <tr class={intervalRowClasses[index]}>
                        <th>{column}</th>
                        <td>{newCardIntervals[0][index]}</td>
                        <td>{newCardIntervals[1][index]}</td>
                    </tr>
                {/each}
            </tbody>
        </table>
    </div>
{/if}

<Warning warning={newCardIntervalsError} className={"alert-warning"} />

<div class="ms-1 me-1">
    <button
        class="btn {computingParams ? 'btn-warning' : 'btn-primary'}"
        disabled={!computingParams && computing}
        on:click={() => computeParams()}
    >
        {#if computingParams}
            {tr.actionsCancel()}
        {:else}
            {tr.deckConfigOptimizeButton()}
        {/if}
    </button>
    <button class="btn btn-primary" on:click={() => computeAllParams()}>
        {tr.deckConfigSaveAndOptimize()}
    </button>
    <div>
        {#if computingParams || checkingParams || checkingHealth || checkingSameDayDecision}
            {computeParamsProgressString}
            {#if computeParamsProgressPct !== undefined}
                <div
                    class="progress fsrs-progress"
                    role="progressbar"
                    aria-valuenow={computeParamsProgressPct}
                    aria-valuemin="0"
                    aria-valuemax="100"
                >
                    <div
                        class="progress-bar"
                        style={`width: ${computeParamsProgressPct}%`}
                    ></div>
                </div>
            {/if}
        {:else if totalReviews !== undefined}
            {tr.statisticsReviews({ reviews: totalReviews })}
        {/if}
    </div>
</div>

<details class="fsrs-advanced m-1">
    <summary>{tr.deckConfigAdvancedSettings()}</summary>

    <div>
        <button
            class="btn btn-outline-primary"
            on:click={() => {
                simulateFsrsRequest.reviewLimit = 9999;
                showSimulatorModal(workloadModal);
            }}
        >
            {tr.deckConfigFsrsDesiredRetentionHelpMeDecideExperimental()}
        </button>
    </div>

    <Warning warning={lastOptimizationWarning} className="alert-warning" />

    {#if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.SEVEN}
        <SwitchRow bind:value={includeSameDayReviewsInFsrs7} defaultValue={true}>
            <SettingTitle>Include same-day reviews in FSRS-7</SettingTitle>
        </SwitchRow>
        <button
            class="btn {checkingSameDayDecision
                ? 'btn-warning'
                : 'btn-outline-primary'}"
            disabled={!checkingSameDayDecision && computing}
            on:click={() => checkSameDayDecision()}
        >
            {#if checkingSameDayDecision}
                {tr.actionsCancel()}
            {:else}
                Same-day reviews: Help Me Decide
            {/if}
        </button>
    {/if}

    <div class="mb-3">
        <SettingTitle>{tr.deckConfigFsrsVersion()}</SettingTitle>
        <select bind:value={$config.fsrsVersion} class="form-select">
            {#each fsrsVersionChoices as choice}
                <option value={choice.value}>{choice.label}</option>
            {/each}
        </select>
    </div>

    {#if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.SIX}
        <ParamsInputRow bind:value={$config.fsrsParams6} defaultValue={[]}>
            <SettingTitle on:click={() => openHelpModal("modelParams")}>
                {tr.deckConfigWeights()}
            </SettingTitle>
        </ParamsInputRow>
    {:else if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.FIVE}
        <ParamsInputRow bind:value={$config.fsrsParams5} defaultValue={[]}>
            <SettingTitle on:click={() => openHelpModal("modelParams")}>
                {tr.deckConfigWeights()}
            </SettingTitle>
        </ParamsInputRow>
    {:else if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.FOUR}
        <ParamsInputRow bind:value={$config.fsrsParams4} defaultValue={[]}>
            <SettingTitle on:click={() => openHelpModal("modelParams")}>
                {tr.deckConfigWeights()}
            </SettingTitle>
        </ParamsInputRow>
    {:else}
        <ParamsInputRow bind:value={$config.fsrsParams7} defaultValue={[]}>
            <SettingTitle on:click={() => openHelpModal("modelParams")}>
                {tr.deckConfigWeights()}
            </SettingTitle>
        </ParamsInputRow>
    {/if}

    {#if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.SEVEN}
        <SwitchRow
            bind:value={$config.fsrsDynamicDesiredRetentionEnabled}
            defaultValue={false}
        >
            <SettingTitle>Dynamic DR (SSP-MMC)</SettingTitle>
        </SwitchRow>

        {#if $config.fsrsDynamicDesiredRetentionEnabled}
            <SwitchRow
                bind:value={$config.fsrsDynamicDesiredRetentionClamp}
                defaultValue={false}
            >
                <SettingTitle>Clamp Unsupported Dynamic DR Targets</SettingTitle>
            </SwitchRow>
            <ParamsInputRow
                bind:value={$config.fsrsDynamicDesiredRetentionParams}
                defaultValue={[]}
                validParamCounts={[0, 15]}
                ariaLabel="Dynamic DR SSP-MMC parameters"
            >
                <SettingTitle>SSP-MMC Parameters</SettingTitle>
            </ParamsInputRow>
            <ParamsInputRow
                bind:value={$config.fsrsDynamicDesiredRetentionWeights}
                defaultValue={[]}
                validParamCounts={dynamicDesiredRetentionCalibrationCounts}
                ariaLabel="Dynamic DR calibration weights"
            >
                <SettingTitle>Calibration Weights</SettingTitle>
            </ParamsInputRow>
            <ParamsInputRow
                bind:value={$config.fsrsDynamicDesiredRetentionAvgDrs}
                defaultValue={[]}
                validParamCounts={dynamicDesiredRetentionCalibrationCounts}
                ariaLabel="Dynamic DR calibration average desired retentions"
            >
                <SettingTitle>Calibration Avg ADR DRs</SettingTitle>
            </ParamsInputRow>
            <div class="dynamic-dr-actions">
                <span>
                    Weight:
                    {dynamicDesiredRetentionWeight === null
                        ? "n/a"
                        : dynamicDesiredRetentionWeight.toFixed(2)}
                </span>
                <span>
                    Bounds:
                    {formatDynamicDrBound($config.fsrsDynamicDesiredRetentionMin)}
                    -
                    {formatDynamicDrBound($config.fsrsDynamicDesiredRetentionMax)}
                </span>
                <button
                    class="btn btn-outline-primary"
                    disabled={!dynamicDesiredRetentionReady}
                    on:click={() => dynamicDesiredRetentionPlotModal?.show()}
                >
                    Visualize DR plot
                </button>
            </div>
            <Warning
                warning={dynamicDesiredRetentionWarning}
                className="alert-warning"
            />
        {/if}
    {/if}

    <ParamsSearchRow bind:value={$config.paramSearch} placeholder={defaultparamSearch}>
        <SettingTitle>Optimize Search Filter</SettingTitle>
    </ParamsSearchRow>
    <ParamsSearchRow
        bind:value={evaluationSearchFilter}
        placeholder={defaultparamSearch}
    >
        <SettingTitle>Evaluation Search Filter</SettingTitle>
    </ParamsSearchRow>

    <SwitchRow bind:value={$fsrsReschedule} defaultValue={false}>
        <SettingTitle on:click={() => openHelpModal("rescheduleCardsOnChange")}>
            <GlobalLabel title={tr.deckConfigRescheduleCardsOnChange()} />
        </SettingTitle>
    </SwitchRow>

    {#if $fsrsReschedule}
        <Warning warning={tr.deckConfigRescheduleCardsWarning()} />
    {/if}

    <SwitchRow bind:value={$healthCheck} defaultValue={false}>
        <SettingTitle on:click={() => openHelpModal("healthCheck")}>
            <GlobalLabel
                title={tr.deckConfigSlowSuffix({ text: tr.deckConfigHealthCheck() })}
            />
        </SettingTitle>
    </SwitchRow>

    <button
        class="btn {checkingHealth ? 'btn-warning' : 'btn-primary'}"
        disabled={!checkingHealth && computing}
        on:click={() => checkHealth()}
    >
        {#if checkingHealth}
            {tr.actionsCancel()}
        {:else}
            {tr.deckConfigHealthCheckButton()}
        {/if}
    </button>
    {#if state.legacyEvaluate}
        <button
            class="btn {checkingParams ? 'btn-warning' : 'btn-primary'}"
            disabled={!checkingParams && computing}
            on:click={() => checkParams()}
        >
            {#if checkingParams}
                {tr.actionsCancel()}
            {:else}
                {tr.deckConfigEvaluateButton()}
            {/if}
        </button>
    {/if}
    <button class="btn btn-primary" on:click={() => showSimulatorModal(simulatorModal)}>
        {tr.deckConfigFsrsSimulatorExperimental()}
    </button>
</details>

<SimulatorModal
    bind:modal={simulatorModal}
    {state}
    {simulateFsrsRequest}
    {computing}
    {openHelpModal}
    {onPresetChange}
/>

<SimulatorModal
    bind:modal={workloadModal}
    workload
    {state}
    {simulateFsrsRequest}
    {computing}
    {openHelpModal}
    {onPresetChange}
/>

<DynamicDesiredRetentionPlotModal
    bind:modal={dynamicDesiredRetentionPlotModal}
    params={$config.fsrsDynamicDesiredRetentionParams}
    calibrationWeights={$config.fsrsDynamicDesiredRetentionWeights}
    calibrationAvgDrs={$config.fsrsDynamicDesiredRetentionAvgDrs}
    fsrsEquivalentWeights={$config.fsrsDynamicDesiredRetentionFsrsEqWeights}
    fsrsEquivalentDrs={$config.fsrsDynamicDesiredRetentionFsrsEqDrs}
    retentionMin={$config.fsrsDynamicDesiredRetentionMin}
    retentionMax={$config.fsrsDynamicDesiredRetentionMax}
    targetAverageDr={effectiveDesiredRetention}
    on:saveTarget={saveDynamicDesiredRetentionPlotTarget}
/>

{#if sameDayDecisionComparison}
    {@const recommendation = sameDayRecommendation(sameDayDecisionComparison)}
    <div class="optimization-popup-backdrop">
        <div class="optimization-popup optimization-popup-wide">
            <div class="optimization-popup-header">Same-Day Review Comparison</div>
            <table class="optimization-popup-table same-day-comparison-table">
                <thead>
                    <tr>
                        <th>Candidate</th>
                        <th>Training targets</th>
                        <th>All log loss</th>
                        <th>All RMSE</th>
                        <th>Long-term log loss</th>
                        <th>Long-term RMSE</th>
                    </tr>
                </thead>
                <tbody>
                    {#each [sameDayDecisionComparison.withSameDay, sameDayDecisionComparison.withoutSameDay] as row}
                        <tr>
                            <th>{row.label}</th>
                            <td>{row.fsrsItems}</td>
                            <td
                                class={sameDayMetricClass(
                                    row.allTargets.logLoss,
                                    row === sameDayDecisionComparison.withSameDay
                                        ? sameDayDecisionComparison.withoutSameDay
                                              .allTargets.logLoss
                                        : sameDayDecisionComparison.withSameDay
                                              .allTargets.logLoss,
                                )}
                            >
                                {formatMetric(row.allTargets.logLoss)}
                            </td>
                            <td
                                class={sameDayMetricClass(
                                    row.allTargets.rmseBins,
                                    row === sameDayDecisionComparison.withSameDay
                                        ? sameDayDecisionComparison.withoutSameDay
                                              .allTargets.rmseBins
                                        : sameDayDecisionComparison.withSameDay
                                              .allTargets.rmseBins,
                                )}
                            >
                                {formatMetric(row.allTargets.rmseBins)}
                            </td>
                            <td
                                class={sameDayMetricClass(
                                    row.longTermTargets.logLoss,
                                    row === sameDayDecisionComparison.withSameDay
                                        ? sameDayDecisionComparison.withoutSameDay
                                              .longTermTargets.logLoss
                                        : sameDayDecisionComparison.withSameDay
                                              .longTermTargets.logLoss,
                                )}
                            >
                                {formatMetric(row.longTermTargets.logLoss)}
                            </td>
                            <td
                                class={sameDayMetricClass(
                                    row.longTermTargets.rmseBins,
                                    row === sameDayDecisionComparison.withSameDay
                                        ? sameDayDecisionComparison.withoutSameDay
                                              .longTermTargets.rmseBins
                                        : sameDayDecisionComparison.withSameDay
                                              .longTermTargets.rmseBins,
                                )}
                            >
                                {formatMetric(row.longTermTargets.rmseBins)}
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
            <div class={`same-day-recommendation ${recommendation.tone}`}>
                {recommendation.text}
            </div>
            <div class="optimization-popup-footer">
                <button
                    class="btn btn-secondary"
                    on:click={() => (sameDayDecisionComparison = undefined)}
                >
                    Close
                </button>
            </div>
        </div>
    </div>
{/if}

{#if optimizationComparison}
    {@const logLossDelta = metricDelta(
        optimizationComparison.current.logLoss,
        optimizationComparison.optimized.logLoss,
    )}
    {@const rmseDelta = metricDelta(
        optimizationComparison.current.rmseBins,
        optimizationComparison.optimized.rmseBins,
    )}
    {@const logLossDeltaPercent = metricDeltaPercent(
        optimizationComparison.current.logLoss,
        optimizationComparison.optimized.logLoss,
    )}
    {@const rmseDeltaPercent = metricDeltaPercent(
        optimizationComparison.current.rmseBins,
        optimizationComparison.optimized.rmseBins,
    )}
    <div class="optimization-popup-backdrop">
        <div class="optimization-popup">
            <div class="optimization-popup-header">Optimization Result</div>
            <table class="optimization-popup-table">
                <thead>
                    <tr>
                        <th>Metric</th>
                        <th>Current</th>
                        <th>Optimized</th>
                        <th>Delta</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <th>Log loss</th>
                        <td>{formatMetric(optimizationComparison.current.logLoss)}</td>
                        <td>
                            {formatMetric(optimizationComparison.optimized.logLoss)}
                        </td>
                        <td class={`optimize-delta ${deltaClass(logLossDelta)}`}>
                            {formatDelta(logLossDelta)}
                            ({formatPercentDelta(logLossDeltaPercent)})
                        </td>
                    </tr>
                    <tr>
                        <th>RMSE (bins)</th>
                        <td>{formatMetric(optimizationComparison.current.rmseBins)}</td>
                        <td>
                            {formatMetric(optimizationComparison.optimized.rmseBins)}
                        </td>
                        <td class={`optimize-delta ${deltaClass(rmseDelta)}`}>
                            {formatDelta(rmseDelta)}
                            ({formatPercentDelta(rmseDeltaPercent)})
                        </td>
                    </tr>
                </tbody>
            </table>
            <div class="optimization-popup-actions">
                <button
                    class="btn btn-outline-primary"
                    disabled={loadingCustomDecayTable ||
                        !supportsCustomDecayTable(
                            optimizationComparison.optimizedParams,
                        )}
                    on:click={loadCustomDecayTable}
                >
                    {#if !supportsCustomDecayTable(optimizationComparison.optimizedParams)}
                        Custom Decay Table (FSRS-7 unsupported)
                    {:else if loadingCustomDecayTable}
                        {tr.actionsProcessing()}
                    {:else}
                        Load Custom Decay Table
                    {/if}
                </button>
            </div>
            {#if customDecayRows.length}
                <table class="optimization-popup-table">
                    <thead>
                        <tr>
                            <th>Decay</th>
                            <th>Log loss</th>
                            <th>Delta</th>
                            <th>RMSE (bins)</th>
                            <th>Delta</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each customDecayRows as row}
                            <tr>
                                <th>
                                    {formatDecay(row.decay)}
                                    {#if row.isBaseline}
                                        (optimized)
                                    {/if}
                                </th>
                                <td>{formatMetric(row.logLoss)}</td>
                                <td
                                    class={`optimize-delta ${deltaClass(row.logLossDelta)}`}
                                >
                                    {formatDelta(row.logLossDelta)}
                                    ({formatPercentDelta(row.logLossDeltaPercent)})
                                </td>
                                <td>{formatMetric(row.rmseBins)}</td>
                                <td
                                    class={`optimize-delta ${deltaClass(row.rmseDelta)}`}
                                >
                                    {formatDelta(row.rmseDelta)}
                                    ({formatPercentDelta(row.rmseDeltaPercent)})
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
            <div class="optimization-popup-footer">
                <button class="btn btn-secondary" on:click={keepCurrentParams}>
                    Keep Current
                </button>
                <button class="btn btn-primary" on:click={applyOptimizedParams}>
                    Use Optimized
                </button>
            </div>
        </div>
    </div>
{/if}

<style>
    .btn {
        margin-bottom: 0.375rem;
    }

    .fsrs-advanced {
        border-top: 1px solid var(--border);
        padding-top: 0.75rem;
    }

    .fsrs-advanced summary {
        cursor: pointer;
        font-weight: 700;
        margin-bottom: 0.75rem;
    }

    .interval-preview {
        margin-bottom: 0.75rem;
        overflow-x: auto;
    }

    .interval-preview-title {
        font-weight: 600;
        margin-bottom: 0.375rem;
    }

    .interval-preview-table {
        width: 100%;
        font-size: 0.9rem;
        border-collapse: collapse;
    }

    .interval-preview-table th,
    .interval-preview-table td {
        padding: 0.35rem 0.5rem;
        border: 1px solid var(--border);
        text-align: left;
        white-space: nowrap;
    }

    .interval-preview-table thead th {
        background: var(--canvas-elevated);
    }

    .interval-preview-table tr.interval-again th,
    .interval-preview-table tr.interval-again td {
        color: var(--fg-red, #b42318);
    }

    .interval-preview-table tr.interval-hard th,
    .interval-preview-table tr.interval-hard td {
        color: var(--fg-orange, #b54708);
    }

    .interval-preview-table tr.interval-good th,
    .interval-preview-table tr.interval-good td {
        color: var(--fg-green, #027a48);
    }

    .interval-preview-table tr.interval-easy th,
    .interval-preview-table tr.interval-easy td {
        color: var(--fg-light-green, #12b76a);
    }

    :global(.two-line) {
        white-space: pre-wrap;
        min-height: calc(2ch + 30px);
        box-sizing: content-box;
        display: flex;
        align-content: center;
        flex-wrap: wrap;
    }

    .optimization-popup-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.45);
        z-index: 1060;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 1rem;
    }

    .optimization-popup {
        width: min(760px, 95vw);
        max-width: calc(100vw - 2rem);
        background: var(--canvas);
        border: 1px solid var(--border);
        border-radius: 0.5rem;
        box-shadow: 0 0.75rem 2.25rem rgba(0, 0, 0, 0.2);
        overflow-x: auto;
    }

    .optimization-popup-wide {
        width: max-content;
    }

    .optimization-popup-header {
        font-weight: 700;
        padding: 0.75rem 1rem 0.5rem;
    }

    .optimization-popup-table {
        width: calc(100% - 2rem);
        margin: 0 1rem 0.75rem;
        border-collapse: collapse;
        font-size: 0.9rem;
    }

    .same-day-comparison-table {
        width: max-content;
        min-width: calc(100% - 2rem);
    }

    .same-day-recommendation {
        margin: 0 1rem 0.75rem;
        padding: 0.5rem 0.75rem;
        border: 1px solid var(--border);
        border-radius: 0.375rem;
        font-size: 0.9rem;
    }

    .same-day-recommendation.better {
        color: var(--fg-green, #027a48);
        border-color: var(--fg-green, #027a48);
    }

    .same-day-recommendation.worse {
        color: var(--fg-red, #b42318);
        border-color: var(--fg-red, #b42318);
    }

    .optimization-popup-table th,
    .optimization-popup-table td {
        border: 1px solid var(--border);
        padding: 0.35rem 0.5rem;
        text-align: right;
        white-space: nowrap;
    }

    .optimization-popup-table th:first-child,
    .optimization-popup-table td:first-child {
        text-align: left;
    }

    .optimization-popup-footer {
        display: flex;
        justify-content: flex-end;
        gap: 0.5rem;
        padding: 0 1rem 1rem;
    }

    .optimization-popup-actions {
        padding: 0 1rem 0.25rem;
    }

    .dynamic-dr-actions {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin: 0.5rem 0 0.75rem;
        font-size: 0.9rem;
    }

    .fsrs-progress {
        width: min(24rem, 100%);
        height: 0.5rem;
        margin-top: 0.35rem;
    }

    .optimize-delta.better {
        color: var(--fg-green, #027a48);
        font-weight: 600;
    }

    .optimize-delta.worse {
        color: var(--fg-red, #b42318);
        font-weight: 600;
    }

    .optimize-delta.equal {
        color: var(--fg, inherit);
    }
</style>
