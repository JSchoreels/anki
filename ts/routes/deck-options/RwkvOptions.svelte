<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckId } from "@generated/anki/decks_pb";
    import { UpdateDeckConfigsMode } from "@generated/anki/deck_config_pb";
    import { Empty } from "@generated/anki/generic_pb";
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
    import RwkvRepeatSpacing from "./RwkvRepeatSpacing.svelte";
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

    let forceBuildingRwkvStateCache = false;
    let recomputingRwkvCalibrationData = false;
    let reschedulingRwkvReviewCards = false;
    $: rwkvActionInProgress =
        forceBuildingRwkvStateCache ||
        recomputingRwkvCalibrationData ||
        reschedulingRwkvReviewCards;

    const settings = {
        rwkvReview: {
            title: tr.deckConfigRwkvReviewEnabled(),
            help: tr.deckConfigRwkvReviewEnabledTooltip(),
        },
        rwkvEnforceGradeOrder: {
            title: tr.deckConfigRwkvReviewEnforceGradeOrder(),
            help: tr.deckConfigRwkvReviewEnforceGradeOrderTooltip(),
        },
        rwkvInstantOrder: {
            title: tr.deckConfigRwkvReviewInstantOrder(),
            help: tr.deckConfigRwkvReviewInstantOrderTooltip(),
        },
        rwkvMinimumReviewsPerDay: {
            title: tr.deckConfigRwkvReviewMinimumReviewsPerDay(),
            help: tr.deckConfigRwkvReviewMinimumReviewsPerDayTooltip(),
        },
        rwkvCandidateRefresh: {
            title: tr.deckConfigRwkvReviewCandidateRefresh(),
            help: tr.deckConfigRwkvReviewCandidateRefreshTooltip(),
        },
        rwkvRefreshInterval: {
            title: tr.deckConfigRwkvReviewRefreshInterval(),
            help: tr.deckConfigRwkvReviewRefreshIntervalTooltip(),
        },
        rwkvFirstReviewElapsed: {
            title: tr.deckConfigRwkvReviewFirstReviewElapsedFromCardCreation(),
            help: tr.deckConfigRwkvReviewFirstReviewElapsedFromCardCreationTooltip(),
        },
        rwkvRepeatSpacing: {
            title: tr.deckConfigRwkvRepeatSpacingTitle(),
            help: tr.deckConfigRwkvRepeatSpacingTooltip(),
        },
    };
    const settingKeys = Object.keys(settings);
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;
    let rwkvWorkloadModal: Modal;

    $: simulateFsrsRequest = buildSimulateFsrsRequest({
        config: $config,
        params: fsrsParams($config, defaults),
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
        <p class="rwkv-description">
            {tr.deckConfigRwkvDescription()}
            <a href="https://github.com/JSchoreels/anki/blob/main/RWKV_FAQ.md">
                {tr.deckConfigRwkvReadMore()}
            </a>
        </p>

        <div class="rwkv-mode-heading">
            <h2>RWKV-Instant</h2>
            <span class="rwkv-mode-badge">{tr.deckConfigRwkvRecommended()}</span>
            <span class="rwkv-mode-subtitle">{tr.deckConfigRwkvInstantSubtitle()}</span>
        </div>
        <p class="rwkv-description">{tr.deckConfigRwkvInstantDescription()}</p>

        <SwitchRow
            bind:value={$config.rwkvReviewInstantOrderEnabled}
            defaultValue={defaults.rwkvReviewInstantOrderEnabled}
        >
            <SettingTitle on:click={() => openSettingHelp("rwkvInstantOrder")}>
                {tr.deckConfigRwkvReviewInstantOrder()}
            </SettingTitle>
            <span class="rwkv-recommendation">
                {tr.deckConfigRwkvReviewInstantOrderRecommended()}
            </span>
        </SwitchRow>

        {#if $config.rwkvReviewInstantOrderEnabled}
            <SpinBoxFloatRow
                bind:value={$config.rwkvReviewMinimumReviewsPerDay}
                defaultValue={defaults.rwkvReviewMinimumReviewsPerDay}
                min={0}
                max={9999}
                step={1}
            >
                <SettingTitle
                    on:click={() => openSettingHelp("rwkvMinimumReviewsPerDay")}
                >
                    {tr.deckConfigRwkvReviewMinimumReviewsPerDay()}
                </SettingTitle>
            </SpinBoxFloatRow>

            <SwitchRow
                bind:value={$config.rwkvReviewCandidateRefreshEnabled}
                defaultValue={defaults.rwkvReviewCandidateRefreshEnabled}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvCandidateRefresh")}>
                    {tr.deckConfigRwkvReviewCandidateRefresh()}
                </SettingTitle>
            </SwitchRow>

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

            <h2 class="rwkv-subheading">Same-Day Repeats</h2>

            <RwkvRepeatSpacing
                bind:reviews={$config.rwkvReviewMinInterveningReviews}
                bind:seconds={$config.rwkvReviewMinElapsedSecs}
                defaultReviews={defaults.rwkvReviewMinInterveningReviews}
                defaultSeconds={defaults.rwkvReviewMinElapsedSecs}
                onHelp={() => openSettingHelp("rwkvRepeatSpacing")}
            />
        {/if}

        <div class="rwkv-mode-heading rwkv-mode-divider">
            <h2>RWKV-Curve</h2>
            <span class="rwkv-mode-subtitle">{tr.deckConfigRwkvCurveSubtitle()}</span>
        </div>
        <p class="rwkv-description">{tr.deckConfigRwkvCurveDescription()}</p>

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

        <Item>
            <SwitchRow
                bind:value={$config.rwkvReviewEnforceGradeOrder}
                defaultValue={defaults.rwkvReviewEnforceGradeOrder}
            >
                <SettingTitle on:click={() => openSettingHelp("rwkvEnforceGradeOrder")}>
                    {tr.deckConfigRwkvReviewEnforceGradeOrder()}
                </SettingTitle>
            </SwitchRow>
        </Item>

        {#if $config.rwkvReviewEnabled}
            <button
                class="btn btn-outline-primary"
                disabled={rwkvActionInProgress}
                on:click={() => rescheduleRwkvReviewCards()}
            >
                {#if reschedulingRwkvReviewCards}
                    Rescheduling Cards with RWKV-Curve Intervals...
                {:else}
                    Reschedule Cards with RWKV-Curve Intervals
                {/if}
            </button>
        {/if}

        {#if $config.rwkvReviewEnabled || $config.rwkvReviewInstantOrderEnabled}
            <h2 class="rwkv-subheading">Maintenance</h2>

            <div class="d-flex flex-wrap gap-2">
                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => forceBuildRwkvStateCache()}
                >
                    {#if forceBuildingRwkvStateCache}
                        Rebuilding RWKV State...
                    {:else}
                        Rebuild RWKV State
                    {/if}
                </button>

                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => recomputeRwkvCalibrationData()}
                >
                    {#if recomputingRwkvCalibrationData}
                        Calculating Calibration Graph Data...
                    {:else}
                        Calculate Calibration Graph Data
                    {/if}
                </button>
            </div>

            <h2 class="rwkv-subheading">Compare</h2>

            <div class="d-flex flex-wrap gap-2">
                <button
                    class="btn btn-outline-primary"
                    disabled={rwkvActionInProgress}
                    on:click={() => showRwkvWorkloadModal()}
                >
                    Compare RWKV with FSRS
                </button>
            </div>
        {/if}
    </DynamicallySlottable>
</TitledContainer>

<SimulatorModal
    bind:modal={rwkvWorkloadModal}
    workload
    rwkvWorkload
    compareWorkloads
    {state}
    {simulateFsrsRequest}
    computing={rwkvActionInProgress}
    openHelpModal={openSettingHelp}
    {onPresetChange}
/>

<style>
    .rwkv-mode-heading {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: 0.375rem 0.625rem;
        margin: 1.25rem 0 0.375rem;
    }

    .rwkv-mode-heading h2 {
        color: var(--fg);
        font-size: 1.0625rem;
        font-weight: 600;
        line-height: 1.4;
        margin: 0;
    }

    .rwkv-mode-subtitle {
        color: var(--fg-subtle);
        font-size: 0.8125rem;
    }

    .rwkv-mode-badge {
        background: var(--canvas-inset);
        border: 1px solid var(--border-subtle);
        border-radius: 1rem;
        color: var(--fg-link);
        font-size: 0.6875rem;
        font-weight: 500;
        line-height: 1.4;
        padding: 0.125rem 0.5rem;
    }

    .rwkv-mode-divider {
        border-top: 1px solid var(--border-subtle);
        margin-top: 1.5rem;
        padding-top: 1.25rem;
    }

    .rwkv-description {
        color: var(--fg-subtle);
        font-size: 0.875rem;
        font-style: italic;
        margin: 0.25rem 0 1rem;
    }

    .rwkv-subheading {
        color: var(--fg-subtle);
        font-size: 0.875rem;
        font-weight: 600;
        margin: 1rem 0 0.25rem;
    }

    .rwkv-recommendation {
        color: var(--fg-subtle);
        display: block;
        font-size: 0.875rem;
    }

    .btn {
        margin-bottom: 0.375rem;
    }
</style>
