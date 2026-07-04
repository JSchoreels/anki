<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckId } from "@generated/anki/decks_pb";
    import { UpdateDeckConfigsMode } from "@generated/anki/deck_config_pb";
    import { Empty, Json } from "@generated/anki/generic_pb";
    import * as tr from "@generated/ftl";
    import { postProto } from "@generated/post";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { commitEditing, type DeckOptionsState, fsrsParams } from "./lib";
    import RwkvBatchSizeRow from "./RwkvBatchSizeRow.svelte";
    import SimulatorModal from "./SimulatorModal.svelte";
    import { buildSimulateFsrsRequest } from "./simulate-fsrs-request";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";

    export let state: DeckOptionsState;
    export let onPresetChange: () => void;

    const config = state.currentConfig;
    const defaults = state.defaults;
    const newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;
    const reviewFuzzEnabled = state.reviewFuzzEnabled;
    const reviewFuzzBase = state.reviewFuzzBase;
    const reviewFuzzFactorShort = state.reviewFuzzFactorShort;
    const reviewFuzzFactorMid = state.reviewFuzzFactorMid;
    const reviewFuzzFactorLong = state.reviewFuzzFactorLong;

    let buildingRwkvStateCache = false;
    let forceBuildingRwkvStateCache = false;
    let recomputingRwkvCalibrationData = false;
    let comparingRwkvExtraFeatureMetrics = false;
    let trainingRwkvCalibration = false;
    let reschedulingRwkvReviewCards = false;
    $: rwkvActionInProgress =
        buildingRwkvStateCache ||
        forceBuildingRwkvStateCache ||
        recomputingRwkvCalibrationData ||
        comparingRwkvExtraFeatureMetrics ||
        trainingRwkvCalibration ||
        reschedulingRwkvReviewCards;

    const settings = {
        rwkvReview: {
            title: tr.deckConfigRwkvReviewEnabled(),
            help: tr.deckConfigRwkvReviewEnabledTooltip(),
        },
        rwkvInstantOrder: {
            title: tr.deckConfigRwkvReviewInstantOrder(),
            help: tr.deckConfigRwkvReviewInstantOrderTooltip(),
        },
        rwkvCandidateRefresh: {
            title: tr.deckConfigRwkvReviewCandidateRefresh(),
            help: tr.deckConfigRwkvReviewCandidateRefreshTooltip(),
        },
        rwkvBatchSize: {
            title: tr.deckConfigRwkvReviewBatchSize(),
            help: tr.deckConfigRwkvReviewBatchSizeTooltip(),
        },
        rwkvRefreshInterval: {
            title: tr.deckConfigRwkvReviewRefreshInterval(),
            help: tr.deckConfigRwkvReviewRefreshIntervalTooltip(),
        },
        rwkvRefreshOnExit: {
            title: tr.deckConfigRwkvReviewRefreshOnExit(),
            help: tr.deckConfigRwkvReviewRefreshOnExitTooltip(),
        },
        rwkvAllowSameDayReview: {
            title: tr.deckConfigRwkvReviewAllowSameDayReview(),
            help: tr.deckConfigRwkvReviewAllowSameDayReviewTooltip(),
        },
        rwkvMinInterveningReviews: {
            title: tr.deckConfigRwkvReviewMinInterveningReviews(),
            help: tr.deckConfigRwkvReviewMinInterveningReviewsTooltip(),
        },
        rwkvMinElapsedSecs: {
            title: tr.deckConfigRwkvReviewMinElapsedSecs(),
            help: tr.deckConfigRwkvReviewMinElapsedSecsTooltip(),
        },
        rwkvDynamicPresetReplay: {
            title: tr.deckConfigRwkvReviewDynamicPresetReplay(),
            help: tr.deckConfigRwkvReviewDynamicPresetReplayTooltip(),
        },
        rwkvPresetTagState: {
            title: tr.deckConfigRwkvReviewPresetTagState(),
            help: tr.deckConfigRwkvReviewPresetTagStateTooltip(),
        },
        rwkvJapaneseFeatureState: {
            title: tr.deckConfigRwkvReviewJapaneseFeatureState(),
            help: tr.deckConfigRwkvReviewJapaneseFeatureStateTooltip(),
        },
        rwkvSelfCorrection: {
            title: tr.deckConfigRwkvReviewSelfCorrection(),
            help: tr.deckConfigRwkvReviewSelfCorrectionTooltip(),
        },
    };
    const settingKeys = Object.keys(settings);
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;
    let rwkvWorkloadModal: Modal;

    $: simulateFsrsRequest = buildSimulateFsrsRequest({
        config: $config,
        params: fsrsParams($config),
        search: `preset:"${state.getCurrentNameForSearch()}" -is:suspended`,
        newCardsIgnoreReviewLimit: $newCardsIgnoreReviewLimit,
        reviewFuzzEnabled: $reviewFuzzEnabled,
        reviewFuzzBase: $reviewFuzzBase,
        reviewFuzzFactorShort: $reviewFuzzFactorShort,
        reviewFuzzFactorMid: $reviewFuzzFactorMid,
        reviewFuzzFactorLong: $reviewFuzzFactorLong,
    });

    function openHelpModal(index: number): void {
        modal.show();
        carousel.to(index);
    }

    function openSettingHelp(key: string): void {
        openHelpModal(settingKeys.indexOf(key));
    }

    async function buildRwkvStateCache(): Promise<void> {
        buildingRwkvStateCache = true;
        try {
            await saveRwkvDeckOptions();
            await postProto("buildRwkvStateCache", new Empty({}), Empty);
        } finally {
            buildingRwkvStateCache = false;
        }
    }

    async function forceBuildRwkvStateCache(): Promise<void> {
        forceBuildingRwkvStateCache = true;
        try {
            await saveRwkvDeckOptions();
            await postProto("forceBuildRwkvStateCache", new Empty({}), Empty);
        } finally {
            forceBuildingRwkvStateCache = false;
        }
    }

    async function recomputeRwkvCalibrationData(): Promise<void> {
        recomputingRwkvCalibrationData = true;
        try {
            await saveRwkvDeckOptions();
            await postProto("recomputeRwkvCalibrationData", new Empty({}), Empty);
        } finally {
            recomputingRwkvCalibrationData = false;
        }
    }

    async function compareRwkvExtraFeatureMetrics(): Promise<void> {
        comparingRwkvExtraFeatureMetrics = true;
        try {
            await commitEditing();
            await postProto(
                "compareRwkvExtraFeatureMetrics",
                rwkvExtraFeatureComparisonRequest(),
                Empty,
            );
        } finally {
            comparingRwkvExtraFeatureMetrics = false;
        }
    }

    function rwkvExtraFeatureComparisonRequest(): Json {
        return new Json({
            json: new TextEncoder().encode(
                JSON.stringify({
                    deckId: state.getTargetDeckId().toString(),
                    configId: state.getCurrentConfigId().toString(),
                    presetTagStateEnabled: $config.rwkvReviewPresetTagStateEnabled,
                    japaneseFeatureStateEnabled:
                        $config.rwkvReviewJapaneseFeatureStateEnabled,
                    selfCorrectionEnabled: $config.rwkvReviewSelfCorrectionEnabled,
                }),
            ),
        });
    }

    async function trainRwkvSelfCorrectionCalibration(): Promise<void> {
        trainingRwkvCalibration = true;
        try {
            await saveRwkvDeckOptions();
            await postProto(
                "trainRwkvSelfCorrectionCalibration",
                rwkvExtraFeatureComparisonRequest(),
                Empty,
            );
        } finally {
            trainingRwkvCalibration = false;
        }
    }

    async function rescheduleRwkvReviewCards(): Promise<void> {
        reschedulingRwkvReviewCards = true;
        try {
            await saveRwkvDeckOptions();
            await postProto(
                "rescheduleRwkvReviewCards",
                new DeckId({ did: state.getTargetDeckId() }),
                Empty,
            );
        } finally {
            reschedulingRwkvReviewCards = false;
        }
    }

    async function saveRwkvDeckOptions(): Promise<void> {
        await commitEditing();
        await state.save(UpdateDeckConfigsMode.NORMAL);
    }

    function showRwkvWorkloadModal(): void {
        simulateFsrsRequest.reviewLimit = 9999;
        rwkvWorkloadModal?.show();
    }
</script>

<TitledContainer title={"RWKV"}>
    <HelpModal
        title={"RWKV"}
        url=""
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} api={{}}>
        <Item>
            <SwitchRow
                bind:value={$config.rwkvReviewEnabled}
                defaultValue={defaults.rwkvReviewEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvReview")}>
                    {tr.deckConfigRwkvReviewEnabled()}
                </SettingTitle>
            </SwitchRow>
        </Item>

        {#if $config.rwkvReviewEnabled}
            <SwitchRow
                bind:value={$config.rwkvReviewInstantOrderEnabled}
                defaultValue={defaults.rwkvReviewInstantOrderEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvInstantOrder")}>
                    {tr.deckConfigRwkvReviewInstantOrder()}
                </SettingTitle>
            </SwitchRow>

            <SwitchRow
                bind:value={$config.rwkvReviewCandidateRefreshEnabled}
                defaultValue={defaults.rwkvReviewCandidateRefreshEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvCandidateRefresh")}>
                    {tr.deckConfigRwkvReviewCandidateRefresh()}
                </SettingTitle>
            </SwitchRow>

            <RwkvBatchSizeRow
                bind:value={$config.rwkvReviewBatchSize}
                defaultValue={defaults.rwkvReviewBatchSize}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvBatchSize")}>
                    {tr.deckConfigRwkvReviewBatchSize()}
                </SettingTitle>
            </RwkvBatchSizeRow>

            <SpinBoxFloatRow
                bind:value={$config.rwkvReviewRefreshInterval}
                defaultValue={defaults.rwkvReviewRefreshInterval}
                min={1}
                max={10000}
                step={1}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvRefreshInterval")}>
                    {tr.deckConfigRwkvReviewRefreshInterval()}
                </SettingTitle>
            </SpinBoxFloatRow>

            <SwitchRow
                bind:value={$config.rwkvReviewRefreshOnExit}
                defaultValue={defaults.rwkvReviewRefreshOnExit}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvRefreshOnExit")}>
                    {tr.deckConfigRwkvReviewRefreshOnExit()}
                </SettingTitle>
            </SwitchRow>

            <SwitchRow
                bind:value={$config.rwkvReviewAllowSameDayReview}
                defaultValue={defaults.rwkvReviewAllowSameDayReview}
            >
                <SettingTitle
                    on:click={() => openSettingHelp("rwkvAllowSameDayReview")}
                >
                    {tr.deckConfigRwkvReviewAllowSameDayReview()}
                </SettingTitle>
            </SwitchRow>

            {#if $config.rwkvReviewAllowSameDayReview}
                <SpinBoxFloatRow
                    bind:value={$config.rwkvReviewMinInterveningReviews}
                    defaultValue={defaults.rwkvReviewMinInterveningReviews}
                    min={0}
                    max={10000}
                    step={1}
                >
                    <SettingTitle
                        on:click={() => openSettingHelp("rwkvMinInterveningReviews")}
                    >
                        {tr.deckConfigRwkvReviewMinInterveningReviews()}
                    </SettingTitle>
                </SpinBoxFloatRow>

                <SpinBoxFloatRow
                    bind:value={$config.rwkvReviewMinElapsedSecs}
                    defaultValue={defaults.rwkvReviewMinElapsedSecs}
                    min={0}
                    max={86400}
                    step={1}
                >
                    <SettingTitle
                        on:click={() => openSettingHelp("rwkvMinElapsedSecs")}
                    >
                        {tr.deckConfigRwkvReviewMinElapsedSecs()}
                    </SettingTitle>
                </SpinBoxFloatRow>
            {/if}

            <SwitchRow
                bind:value={$config.rwkvReviewDynamicPresetReplay}
                defaultValue={defaults.rwkvReviewDynamicPresetReplay}
            >
                <SettingTitle
                    on:click={() => openSettingHelp("rwkvDynamicPresetReplay")}
                >
                    {tr.deckConfigRwkvReviewDynamicPresetReplay()}
                </SettingTitle>
            </SwitchRow>

            <SwitchRow
                bind:value={$config.rwkvReviewPresetTagStateEnabled}
                defaultValue={defaults.rwkvReviewPresetTagStateEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvPresetTagState")}>
                    {tr.deckConfigRwkvReviewPresetTagState()}
                </SettingTitle>
            </SwitchRow>

            <SwitchRow
                bind:value={$config.rwkvReviewJapaneseFeatureStateEnabled}
                defaultValue={defaults.rwkvReviewJapaneseFeatureStateEnabled}
            >
                <SettingTitle
                    on:click={() => openSettingHelp("rwkvJapaneseFeatureState")}
                >
                    {tr.deckConfigRwkvReviewJapaneseFeatureState()}
                </SettingTitle>
            </SwitchRow>

            <SwitchRow
                bind:value={$config.rwkvReviewSelfCorrectionEnabled}
                defaultValue={defaults.rwkvReviewSelfCorrectionEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvSelfCorrection")}>
                    {tr.deckConfigRwkvReviewSelfCorrection()}
                </SettingTitle>
            </SwitchRow>

            <div class="d-flex flex-wrap gap-2">
                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => buildRwkvStateCache()}
                >
                    {#if buildingRwkvStateCache}
                        Starting RWKV state cache build...
                    {:else}
                        Build RWKV state cache
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => forceBuildRwkvStateCache()}
                >
                    {#if forceBuildingRwkvStateCache}
                        Starting full RWKV state cache rebuild...
                    {:else}
                        Force rebuild RWKV state cache
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => recomputeRwkvCalibrationData()}
                >
                    {#if recomputingRwkvCalibrationData}
                        Starting RWKV calibration recompute...
                    {:else}
                        Recompute RWKV Calibration Data
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => compareRwkvExtraFeatureMetrics()}
                >
                    {#if comparingRwkvExtraFeatureMetrics}
                        Computing RWKV feature metrics...
                    {:else}
                        Compare RWKV Extra Features
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => trainRwkvSelfCorrectionCalibration()}
                >
                    {#if trainingRwkvCalibration}
                        Starting RWKV calibration training...
                    {:else}
                        Train RWKV Calibration
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => rescheduleRwkvReviewCards()}
                >
                    {#if reschedulingRwkvReviewCards}
                        Starting RWKV reschedule...
                    {:else}
                        Reschedule & update RWKV memory state
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => showRwkvWorkloadModal()}
                >
                    RWKV Desired Retention: Help Me Decide
                </button>
            </div>
        {/if}
    </DynamicallySlottable>
</TitledContainer>

<SimulatorModal
    bind:modal={rwkvWorkloadModal}
    workload
    rwkvWorkload
    {state}
    {simulateFsrsRequest}
    computing={rwkvActionInProgress}
    openHelpModal={openSettingHelp}
    {onPresetChange}
/>

<style>
    .btn {
        margin-bottom: 0.375rem;
    }
</style>
