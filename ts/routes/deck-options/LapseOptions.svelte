<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckConfig_Config_LeechAction } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    import { leechChoices, reviewMixChoices, reviewOrderChoices } from "./choices";
    import Choice from "$lib/components/EnumSelector.svelte";
    import InlineNumber from "./InlineNumber.svelte";
    import StepsInput from "./StepsInput.svelte";
    import {
        readDisplayOrderSettings,
        withDisplayOrderSettings,
    } from "./display-order-settings";
    import { usesSameDayOrder } from "./study-preview";
    import GlobalLabel from "./GlobalLabel.svelte";
    import type { DeckOptionsState } from "./lib";
    import SpinBoxRow from "./SpinBoxRow.svelte";
    import StepsInputRow from "./StepsInputRow.svelte";
    import Warning from "./Warning.svelte";

    export let state: DeckOptionsState;
    export let study = false;
    export let api = {};

    const config = state.currentConfig;
    const aux = state.currentAuxData;
    $: sameDayOrder = usesSameDayOrder($config, $fsrs);
    const defaults = state.defaults;
    const fsrs = state.fsrs;
    const fsrsShortTermWithStepsEnabled = state.fsrsShortTermWithStepsEnabled;
    const fsrsLearningQueuesDisabled = state.fsrsLearningQueuesDisabled;

    let stepsExceedMinimumInterval: string;
    let stepsTooLargeForFsrs: string;
    $: {
        const lastRelearnStepInDays = $config.relearnSteps.length
            ? $config.relearnSteps[$config.relearnSteps.length - 1] / 60 / 24
            : 0;
        stepsExceedMinimumInterval =
            !$fsrs && lastRelearnStepInDays > $config.minimumLapseInterval
                ? tr.deckConfigRelearningStepsAboveMinimumInterval()
                : "";
        stepsTooLargeForFsrs =
            $fsrs && lastRelearnStepInDays >= 1
                ? tr.deckConfigStepsTooLargeForFsrs()
                : "";
    }

    const settings = {
        relearningSteps: {
            title: tr.deckConfigRelearningSteps(),
            help: tr.deckConfigRelearningStepsTooltip(),
            url: HelpPage.DeckOptions.relearningSteps,
        },
        minimumInterval: {
            title: tr.schedulingMinimumInterval(),
            help: tr.deckConfigMinimumIntervalTooltip(),
            url: HelpPage.DeckOptions.minimumInterval,
            sched: HelpItemScheduler.SM2,
        },
        leechThreshold: {
            title: tr.schedulingLeechThreshold(),
            help: tr.deckConfigLeechThresholdTooltip(),
            url: HelpPage.Leeches.leeches,
        },
        leechAction: {
            title: tr.schedulingLeechAction(),
            help: tr.deckConfigLeechActionTooltip(),
            url: HelpPage.Leeches.waiting,
        },
        leechOnlyIfYoung: {
            title: tr.deckConfigLeechOnlyIfYoung(),
            help: tr.deckConfigLeechOnlyIfYoungTooltip(),
            url: HelpPage.Leeches.waiting,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;

    function openHelpModal(index: number): void {
        modal.show();
        carousel.to(index);
    }
</script>

<TitledContainer title={study ? tr.deckConfigStudyReviews() : tr.schedulingLapses()}>
    <HelpModal
        title={tr.schedulingLapses()}
        url={HelpPage.DeckOptions.lapses}
        slot="tooltip"
        fsrs={$fsrs}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        {#if study}
            <Item>
                <div class="study-line">
                    <span>
                        {tr.deckConfigStudyPrioritize()}
                        <Choice
                            inlineValue
                            accessibleLabel={tr.deckConfigReviewSortOrder()}
                            choices={reviewOrderChoices($fsrs)}
                            bind:value={$config.reviewOrder}
                        />
                    </span>
                </div>
            </Item>
            <Item>
                <div class="study-line">
                    <span>
                        {tr.deckConfigStudyCarriedSteps()}
                        <Choice
                            inlineValue
                            accessibleLabel={tr.deckConfigStudyCarriedSteps()}
                            choices={reviewMixChoices()}
                            bind:value={$config.interdayLearningMix}
                        />
                    </span>
                    <span>
                        {tr.deckConfigStudySameDaySteps()}
                        {#if sameDayOrder}
                            <Choice
                                inlineValue
                                accessibleLabel={tr.deckConfigStudySameDaySteps()}
                                choices={reviewMixChoices()}
                                value={readDisplayOrderSettings($aux)
                                    .intradayLearningMix}
                                on:change={(e) => {
                                    const changed = withDisplayOrderSettings($aux, {
                                        intradayLearningMix: e.detail.value,
                                    });
                                    if (changed) {
                                        aux.set(changed);
                                    }
                                }}
                            />
                        {:else}<span class="study-muted">
                                {tr.deckConfigStudyWhenDue()}
                            </span>{/if}
                    </span>
                </div>
            </Item>
            <Item>
                <div class="study-line">
                    <span>
                        {tr.deckConfigStudyRelearn()}
                        <StepsInput
                            inlineLabel={settings.relearningSteps.title}
                            bind:value={$config.relearnSteps}
                        />
                    </span>
                    <span>
                        {settings.leechThreshold.title}
                        <InlineNumber
                            label={settings.leechThreshold.title}
                            min={1}
                            bind:value={$config.leechThreshold}
                        />
                        <Choice
                            inlineValue
                            accessibleLabel={settings.leechAction.title}
                            choices={leechChoices()}
                            bind:value={$config.leechAction}
                        />
                    </span>
                    {#if $config.leechAction === DeckConfig_Config_LeechAction.SUSPEND}
                        <label title={settings.leechOnlyIfYoung.help}>
                            <input
                                type="checkbox"
                                bind:checked={$config.leechOnlyIfYoung}
                            />
                            {settings.leechOnlyIfYoung.title}
                        </label>
                    {/if}
                </div>
            </Item>
            <Item>
                <div class="study-line checks">
                    {#if $fsrs || $config.rwkvReviewInstantOrderEnabled}<label>
                            <input
                                type="checkbox"
                                bind:checked={$fsrsShortTermWithStepsEnabled}
                            />
                            {tr.deckConfigStudyAllowSteps()}
                        </label>{/if}
                    {#if $fsrs}<label>
                            <input
                                type="checkbox"
                                bind:checked={$fsrsLearningQueuesDisabled}
                            />
                            {tr.deckConfigStudySkipSteps()}
                        </label>
                    {:else}<span>
                            {settings.minimumInterval.title}
                            <InlineNumber
                                label={settings.minimumInterval.title}
                                min={1}
                                bind:value={$config.minimumLapseInterval}
                            />
                        </span>{/if}
                </div>
            </Item>
            <Warning warning={stepsTooLargeForFsrs} /><Warning
                warning={stepsExceedMinimumInterval}
            />
        {:else}
            <Item>
                <StepsInputRow
                    bind:value={$config.relearnSteps}
                    defaultValue={defaults.relearnSteps}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("relearningSteps"),
                            )}
                    >
                        {settings.relearningSteps.title}
                    </SettingTitle>
                </StepsInputRow>
            </Item>

            <Item>
                <Warning warning={stepsTooLargeForFsrs} />
            </Item>

            {#if $fsrs || $config.rwkvReviewInstantOrderEnabled}
                <Item>
                    <SwitchRow
                        bind:value={$fsrsShortTermWithStepsEnabled}
                        defaultValue={true}
                    >
                        <SettingTitle>
                            <GlobalLabel
                                title={"Allow same day review for (re)learning steps"}
                            />
                        </SettingTitle>
                    </SwitchRow>
                </Item>
            {/if}

            {#if $fsrs}
                <Item>
                    <SwitchRow
                        bind:value={$fsrsLearningQueuesDisabled}
                        defaultValue={false}
                    >
                        <SettingTitle>
                            <GlobalLabel
                                title={"Skip learning/relearning queues with FSRS/RWKV"}
                            />
                        </SettingTitle>
                    </SwitchRow>
                </Item>
            {/if}

            {#if !$fsrs}
                <Item>
                    <SpinBoxRow
                        bind:value={$config.minimumLapseInterval}
                        defaultValue={defaults.minimumLapseInterval}
                        min={1}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    Object.keys(settings).indexOf("minimumInterval"),
                                )}
                        >
                            {settings.minimumInterval.title}
                        </SettingTitle>
                    </SpinBoxRow>
                </Item>
            {/if}

            <Item>
                <Warning warning={stepsExceedMinimumInterval} />
            </Item>

            <Item>
                <SpinBoxRow
                    bind:value={$config.leechThreshold}
                    defaultValue={defaults.leechThreshold}
                    min={1}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("leechThreshold"),
                            )}
                    >
                        {settings.leechThreshold.title}
                    </SettingTitle>
                </SpinBoxRow>
            </Item>

            <Item>
                <EnumSelectorRow
                    bind:value={$config.leechAction}
                    defaultValue={defaults.leechAction}
                    choices={leechChoices()}
                    breakpoint="md"
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(Object.keys(settings).indexOf("leechAction"))}
                    >
                        {settings.leechAction.title}
                    </SettingTitle>
                </EnumSelectorRow>
            </Item>

            {#if $config.leechAction === DeckConfig_Config_LeechAction.SUSPEND}
                <Item>
                    <SwitchRow
                        bind:value={$config.leechOnlyIfYoung}
                        defaultValue={defaults.leechOnlyIfYoung}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    Object.keys(settings).indexOf("leechOnlyIfYoung"),
                                )}
                        >
                            {settings.leechOnlyIfYoung.title}
                        </SettingTitle>
                    </SwitchRow>
                </Item>
            {/if}
        {/if}
    </DynamicallySlottable>
    <slot />
</TitledContainer>
