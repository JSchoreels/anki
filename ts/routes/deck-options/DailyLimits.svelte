<!--
    Copyright: Ankitects Pty Ltd and contributors
    License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import CheckBox from "$lib/components/CheckBox.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { suspendedCardOrderChoices } from "./choices";
    import GlobalLabel from "./GlobalLabel.svelte";
    import InlineNumber from "./InlineNumber.svelte";
    import type { DeckOptionsState } from "./lib";
    import { ValueTab } from "./lib";
    import SpinBoxRow from "./SpinBoxRow.svelte";
    import TabbedValue from "./TabbedValue.svelte";
    import Warning from "./Warning.svelte";

    export let state: DeckOptionsState;
    export let study = false;
    export let api: Record<string, never>;

    export function onPresetChange() {
        newTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.newPerDay,
            (value) => ($config.newPerDay = value!),
            $config.newPerDay,
            null,
        );
        reviewTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.reviewsPerDay,
            (value) => ($config.reviewsPerDay = value!),
            $config.reviewsPerDay,
            null,
        );
    }

    const config = state.currentConfig;
    const limits = state.deckLimits;
    const defaults = state.defaults;
    const newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;
    const applyAllParentLimits = state.applyAllParentLimits;
    const rwkvRespectSubdeckMinimums = state.rwkvRespectSubdeckMinimums;
    const rwkvApplyRespectSubdeckMinimumsToChildren =
        state.rwkvApplyRespectSubdeckMinimumsToChildren;

    const v3Extra =
        "\n\n" + tr.deckConfigLimitDeckV3() + "\n\n" + tr.deckConfigTabDescription();
    const reviewV3Extra = "\n\n" + tr.deckConfigLimitInterdayBoundByReviews() + v3Extra;
    const newCardsIgnoreReviewLimitHelp =
        tr.deckConfigAffectsEntireCollection() +
        "\n\n" +
        tr.deckConfigNewCardsIgnoreReviewLimitTooltip();
    const applyAllParentLimitsHelp =
        tr.deckConfigAffectsEntireCollection() +
        "\n\n" +
        tr.deckConfigApplyAllParentLimitsTooltip();

    $: reviewsTooLow =
        Math.min(9999, newValue * 10) > reviewsValue
            ? tr.deckConfigReviewsTooLow({
                  cards: newValue,
                  expected: Math.min(9999, newValue * 10),
              })
            : "";

    const newTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.newPerDay,
            (value) => ($config.newPerDay = value!),
            $config.newPerDay,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.new ?? null,
            (value) => ($limits.new = value ?? undefined),
            null,
            null,
        ),
        new ValueTab(
            tr.deckConfigTodayOnly(),
            $limits.newTodayActive ? ($limits.newToday ?? null) : null,
            (value) => ($limits.newToday = value ?? undefined),
            null,
            $limits.newToday ?? null,
        ),
    ];

    const reviewTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.reviewsPerDay,
            (value) => ($config.reviewsPerDay = value!),
            $config.reviewsPerDay,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.review ?? null,
            (value) => ($limits.review = value ?? undefined),
            null,
            null,
        ),
        new ValueTab(
            tr.deckConfigTodayOnly(),
            $limits.reviewTodayActive ? ($limits.reviewToday ?? null) : null,
            (value) => ($limits.reviewToday = value ?? undefined),
            null,
            $limits.reviewToday ?? null,
        ),
    ];

    let newValue = 0;
    let reviewsValue = 0;

    const settings = {
        newLimit: {
            title: tr.schedulingNewCardsday(),
            help: tr.deckConfigNewLimitTooltip() + v3Extra,
            url: HelpPage.DeckOptions.newCardsday,
        },
        reviewLimit: {
            title: tr.schedulingMaximumReviewsday(),
            help: tr.deckConfigReviewLimitTooltip() + reviewV3Extra,
            url: HelpPage.DeckOptions.maximumReviewsday,
        },
        rwkvMinimumReviewsPerDay: {
            title: tr.deckConfigRwkvReviewMinimumReviewsPerDay(),
            help: tr.deckConfigRwkvReviewMinimumReviewsPerDayTooltip(),
        },
        rwkvNewCardsIgnoreMinimumReviews: {
            title: tr.deckConfigRwkvReviewNewCardsIgnoreMinimumReviews(),
            help: tr.deckConfigRwkvReviewNewCardsIgnoreMinimumReviewsTooltip(),
        },
        rwkvRespectSubdeckMinimums: {
            title: tr.deckConfigRwkvReviewRespectSubdeckMinimums(),
            help: tr.deckConfigRwkvReviewRespectSubdeckMinimumsTooltip(),
        },
        includeSuspendedCards: {
            title: tr.deckConfigIncludeSuspendedCardsInLearnablePool(),
            help: tr.deckConfigIncludeSuspendedCardsInLearnablePoolTooltip(),
            url: HelpPage.DeckOptions.newCardsday,
        },
        suspendedCardOrder: {
            title: tr.deckConfigSuspendedCardOrder(),
            help: tr.deckConfigSuspendedCardOrderTooltip(),
            url: HelpPage.DeckOptions.newCardsday,
        },
        sameDayReviewsIgnoreReviewLimit: {
            title: tr.deckConfigSameDayReviewsIgnoreReviewLimit(),
            help: tr.deckConfigSameDayReviewsIgnoreReviewLimitTooltip(),
            url: HelpPage.DeckOptions.maximumReviewsday,
        },
        newCardsIgnoreReviewLimit: {
            title: tr.deckConfigNewCardsIgnoreReviewLimit(),
            help: newCardsIgnoreReviewLimitHelp,
            url: HelpPage.DeckOptions.newCardsday,
            global: true,
        },
        applyAllParentLimits: {
            title: tr.deckConfigApplyAllParentLimits(),
            help: applyAllParentLimitsHelp,
            url: HelpPage.DeckOptions.limitsFromTop,
            global: true,
        },
    };
    const rwkvSettingKeys = [
        "rwkvMinimumReviewsPerDay",
        "rwkvNewCardsIgnoreMinimumReviews",
        "rwkvRespectSubdeckMinimums",
    ];
    // The RWKV entries are only listed while RWKV-Instant is on, so help
    // indices are always computed from the filtered keys.
    $: settingKeys = Object.keys(settings).filter(
        (key) =>
            $config.rwkvReviewInstantOrderEnabled || !rwkvSettingKeys.includes(key),
    );
    $: helpSections = settingKeys.map((key) => settings[key]) as HelpItem[];
    $: minimumIsZero = !$config.rwkvReviewMinimumReviewsPerDay;

    let modal: Modal;
    let carousel: Carousel;

    function openHelpModal(index: number): void {
        modal.show();
        carousel.to(index);
    }
</script>

<TitledContainer title={tr.deckConfigDailyLimits()}>
    <HelpModal
        title={tr.deckConfigDailyLimits()}
        url={HelpPage.DeckOptions.dailyLimits}
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        {#if study}
            <Item>
                <div class="study-line limits">
                    <span>
                        {tr.deckConfigStudyNewLimit()}
                        <InlineNumber
                            label={settings.newLimit.title}
                            bind:value={newValue}
                        />
                        <TabbedValue
                            inlineLabel={tr.deckConfigStudyNewScope()}
                            tabs={newTabs}
                            bind:value={newValue}
                        />
                    </span>
                    {#if $config.rwkvReviewInstantOrderEnabled}
                        <span
                            title={tr.deckConfigRwkvReviewMinimumReviewsPerDayTooltip()}
                        >
                            {tr.deckConfigStudyMinimum()}
                            <InlineNumber
                                label={tr.deckConfigRwkvReviewMinimumReviewsPerDay()}
                                bind:value={$config.rwkvReviewMinimumReviewsPerDay}
                            /> (RWKV-Instant)
                        </span>
                    {/if}
                    <span>
                        {tr.deckConfigStudyMaximum()}
                        <InlineNumber
                            label={settings.reviewLimit.title}
                            bind:value={reviewsValue}
                        />
                        <TabbedValue
                            inlineLabel={tr.deckConfigStudyReviewScope()}
                            tabs={reviewTabs}
                            bind:value={reviewsValue}
                        />
                    </span>
                </div>
            </Item>
            <Item>
                <div class="study-line checks">
                    <label title={newCardsIgnoreReviewLimitHelp}>
                        <input
                            type="checkbox"
                            bind:checked={$newCardsIgnoreReviewLimit}
                        />
                        {settings.newCardsIgnoreReviewLimit.title}
                    </label>
                    {#if $config.rwkvReviewInstantOrderEnabled}
                        <label title={settings.rwkvNewCardsIgnoreMinimumReviews.help}>
                            <input
                                type="checkbox"
                                disabled={minimumIsZero}
                                bind:checked={
                                    $config.rwkvReviewNewCardsIgnoreMinimumReviews
                                }
                            />
                            {settings.rwkvNewCardsIgnoreMinimumReviews.title}
                        </label>
                        <label title={settings.rwkvRespectSubdeckMinimums.help}>
                            <input
                                type="checkbox"
                                bind:checked={$rwkvRespectSubdeckMinimums}
                            />
                            {settings.rwkvRespectSubdeckMinimums.title}
                        </label>
                        <label>
                            <input
                                type="checkbox"
                                bind:checked={
                                    $rwkvApplyRespectSubdeckMinimumsToChildren
                                }
                            />
                            {tr.deckConfigRwkvApplyRespectSubdeckMinimumsToChildren()}
                        </label>
                    {/if}
                    <label title={settings.sameDayReviewsIgnoreReviewLimit.help}>
                        <input
                            type="checkbox"
                            bind:checked={$config.sameDayReviewsIgnoreReviewLimit}
                        />
                        {settings.sameDayReviewsIgnoreReviewLimit.title}
                    </label>
                    <label title={applyAllParentLimitsHelp}>
                        <input type="checkbox" bind:checked={$applyAllParentLimits} />
                        {settings.applyAllParentLimits.title}
                    </label>
                </div>
            </Item>
            <Warning warning={reviewsTooLow} />
        {:else}
            <Item>
                <SpinBoxRow bind:value={newValue} defaultValue={defaults.newPerDay}>
                    <TabbedValue slot="tabs" tabs={newTabs} bind:value={newValue} />
                    <SettingTitle
                        on:click={() => openHelpModal(settingKeys.indexOf("newLimit"))}
                    >
                        {settings.newLimit.title}
                    </SettingTitle>
                </SpinBoxRow>
            </Item>

            <Item>
                <SpinBoxRow
                    bind:value={reviewsValue}
                    defaultValue={defaults.reviewsPerDay}
                >
                    <TabbedValue
                        slot="tabs"
                        tabs={reviewTabs}
                        bind:value={reviewsValue}
                    />
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(settingKeys.indexOf("reviewLimit"))}
                    >
                        {settings.reviewLimit.title}
                    </SettingTitle>
                </SpinBoxRow>
            </Item>

            {#if $config.rwkvReviewInstantOrderEnabled}
                <Item>
                    <SpinBoxRow
                        bind:value={$config.rwkvReviewMinimumReviewsPerDay}
                        defaultValue={defaults.rwkvReviewMinimumReviewsPerDay}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    settingKeys.indexOf("rwkvMinimumReviewsPerDay"),
                                )}
                        >
                            {settings.rwkvMinimumReviewsPerDay.title}
                        </SettingTitle>
                    </SpinBoxRow>
                </Item>

                <Item>
                    <SwitchRow
                        bind:value={$config.rwkvReviewNewCardsIgnoreMinimumReviews}
                        defaultValue={defaults.rwkvReviewNewCardsIgnoreMinimumReviews}
                        disabled={minimumIsZero}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    settingKeys.indexOf(
                                        "rwkvNewCardsIgnoreMinimumReviews",
                                    ),
                                )}
                        >
                            {settings.rwkvNewCardsIgnoreMinimumReviews.title}
                        </SettingTitle>
                    </SwitchRow>
                </Item>

                <Item>
                    <SwitchRow
                        bind:value={$rwkvRespectSubdeckMinimums}
                        defaultValue={false}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    settingKeys.indexOf("rwkvRespectSubdeckMinimums"),
                                )}
                        >
                            {settings.rwkvRespectSubdeckMinimums.title}
                        </SettingTitle>
                    </SwitchRow>
                    <CheckBox bind:value={$rwkvApplyRespectSubdeckMinimumsToChildren}>
                        {tr.deckConfigRwkvApplyRespectSubdeckMinimumsToChildren()}
                    </CheckBox>
                </Item>
            {/if}

            <Item>
                <Warning warning={reviewsTooLow} />
            </Item>

            <Item>
                <SwitchRow
                    bind:value={$config.includeSuspendedCardsInLearnablePool}
                    defaultValue={defaults.includeSuspendedCardsInLearnablePool}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(settingKeys.indexOf("includeSuspendedCards"))}
                    >
                        {settings.includeSuspendedCards.title}
                    </SettingTitle>
                </SwitchRow>
            </Item>

            {#if $config.includeSuspendedCardsInLearnablePool}
                <Item>
                    <EnumSelectorRow
                        bind:value={$config.suspendedCardOrder}
                        defaultValue={defaults.suspendedCardOrder}
                        choices={suspendedCardOrderChoices()}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    settingKeys.indexOf("suspendedCardOrder"),
                                )}
                        >
                            {settings.suspendedCardOrder.title}
                        </SettingTitle>
                    </EnumSelectorRow>
                </Item>
            {/if}

            <Item>
                <SwitchRow
                    bind:value={$config.sameDayReviewsIgnoreReviewLimit}
                    defaultValue={defaults.sameDayReviewsIgnoreReviewLimit}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                settingKeys.indexOf("sameDayReviewsIgnoreReviewLimit"),
                            )}
                    >
                        {settings.sameDayReviewsIgnoreReviewLimit.title}
                    </SettingTitle>
                </SwitchRow>
            </Item>

            <Item>
                <SwitchRow bind:value={$newCardsIgnoreReviewLimit} defaultValue={false}>
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                settingKeys.indexOf("newCardsIgnoreReviewLimit"),
                            )}
                    >
                        <GlobalLabel title={settings.newCardsIgnoreReviewLimit.title} />
                    </SettingTitle>
                </SwitchRow>
            </Item>

            <Item>
                <SwitchRow bind:value={$applyAllParentLimits} defaultValue={false}>
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(settingKeys.indexOf("applyAllParentLimits"))}
                    >
                        <GlobalLabel title={settings.applyAllParentLimits.title} />
                    </SettingTitle>
                </SwitchRow>
            </Item>
        {/if}
    </DynamicallySlottable>
</TitledContainer>
