<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        DeckConfig_Config_NewCardGatherPriority as GatherOrder,
        DeckConfig_Config_NewCardSortOrder as SortOrder,
        DeckConfig_Config_ReviewMix as ReviewMix,
    } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import {
        newGatherPriorityChoices,
        newSortOrderChoices,
        reviewMixChoices,
        reviewOrderChoices,
    } from "./choices";
    import {
        readDisplayOrderSettings,
        withDisplayOrderSettings,
    } from "./display-order-settings";
    import type { DeckOptionsState } from "./lib";
    import SpinBoxRow from "./SpinBoxRow.svelte";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    const config = state.currentConfig;
    const defaults = state.defaults;
    const fsrs = state.fsrs;
    const auxData = state.currentAuxData;

    let intradayLearningMix = ReviewMix.MIX_WITH_REVIEWS;
    $: intradayLearningMix = readDisplayOrderSettings($auxData).intradayLearningMix;
    $: {
        const updated = withDisplayOrderSettings($auxData, {
            intradayLearningMix,
        });
        if (updated) {
            auxData.set(updated);
        }
    }

    // kuma3: kept in the preset's extra JSON, like the intraday mix above
    const KUMA3_KEY = "kuma3.deckoptions";
    function kuma3Options(aux: Record<string, unknown>): Record<string, unknown> {
        const value = aux[KUMA3_KEY];
        return typeof value === "object" && value !== null && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
    }
    const storedReviewsPerNewCard = (aux: Record<string, unknown>) =>
        Number(kuma3Options(aux).reviews_per_new_card) || 0;
    let reviewsPerNewCard = 0;
    $: reviewsPerNewCard = storedReviewsPerNewCard($auxData);
    $: if (storedReviewsPerNewCard($auxData) !== reviewsPerNewCard) {
        auxData.set({
            ...$auxData,
            [KUMA3_KEY]: {
                ...kuma3Options($auxData),
                reviews_per_new_card: reviewsPerNewCard,
            },
        });
    }

    const currentDeck = "\n\n" + tr.deckConfigDisplayOrderWillUseCurrentDeck();

    let disabledNewSortOrders: number[] = [];
    $: {
        switch ($config.newCardGatherPriority) {
            case GatherOrder.RANDOM_NOTES:
                disabledNewSortOrders = [
                    // same as TEMPLATE
                    SortOrder.TEMPLATE_THEN_RANDOM,
                    // same as NO_SORT
                    SortOrder.RANDOM_NOTE_THEN_TEMPLATE,
                ];
                break;
            case GatherOrder.RANDOM_CARDS:
                disabledNewSortOrders = [
                    // same as TEMPLATE
                    SortOrder.TEMPLATE_THEN_RANDOM,
                    // not useful if siblings are not gathered together
                    SortOrder.RANDOM_NOTE_THEN_TEMPLATE,
                    // same as NO_SORT
                    SortOrder.RANDOM_CARD,
                ];
                break;
            default:
                disabledNewSortOrders = [];
                break;
        }

        // disabled options aren't deselected automatically
        if (disabledNewSortOrders.includes($config.newCardSortOrder)) {
            // default option should never be disabled
            $config.newCardSortOrder = 0;
        }

        // check for invalid index from previous version
        if (!($config.newCardSortOrder in SortOrder)) {
            $config.newCardSortOrder = 0;
        }
    }

    const settings = {
        newGatherPriority: {
            title: tr.deckConfigNewGatherPriority(),
            help: tr.deckConfigNewGatherPriorityTooltip2() + currentDeck,
        },
        newCardSortOrder: {
            title: tr.deckConfigNewCardSortOrder(),
            help: tr.deckConfigNewCardSortOrderTooltip2() + currentDeck,
        },
        newReviewPriority: {
            title: tr.deckConfigNewReviewPriority(),
            help: tr.deckConfigNewReviewPriorityTooltip() + currentDeck,
        },
        reviewsPerNewCard: {
            title: tr.deckConfigReviewsPerNewCard(),
            help: tr.deckConfigReviewsPerNewCardTooltip() + currentDeck,
        },
        interdayStepPriority: {
            title: tr.deckConfigInterdayStepPriority(),
            help: tr.deckConfigInterdayStepPriorityTooltip() + currentDeck,
        },
        intradayStepPriority: {
            title: tr.deckConfigIntradayStepPriority(),
            help: tr.deckConfigIntradayStepPriorityTooltip() + currentDeck,
        },
        reviewSortOrder: {
            title: tr.deckConfigReviewSortOrder(),
            help: tr.deckConfigReviewSortOrderTooltip() + currentDeck,
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

<TitledContainer title={tr.deckConfigOrderingTitle()}>
    <HelpModal
        title={tr.deckConfigOrderingTitle()}
        url={HelpPage.DeckOptions.displayOrder}
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <Item>
            <EnumSelectorRow
                bind:value={$config.newCardGatherPriority}
                defaultValue={defaults.newCardGatherPriority}
                choices={newGatherPriorityChoices()}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("newGatherPriority"),
                        )}
                >
                    {settings.newGatherPriority.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        <Item>
            <EnumSelectorRow
                bind:value={$config.newCardSortOrder}
                defaultValue={defaults.newCardSortOrder}
                choices={newSortOrderChoices()}
                disabledChoices={disabledNewSortOrders}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("newCardSortOrder"),
                        )}
                >
                    {settings.newCardSortOrder.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        <Item>
            <EnumSelectorRow
                bind:value={$config.newMix}
                defaultValue={defaults.newMix}
                choices={reviewMixChoices()}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("newReviewPriority"),
                        )}
                >
                    {settings.newReviewPriority.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        {#if $config.newMix === ReviewMix.MIX_WITH_REVIEWS}
            <Item>
                <SpinBoxRow bind:value={reviewsPerNewCard} defaultValue={0}>
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("reviewsPerNewCard"),
                            )}
                    >
                        {settings.reviewsPerNewCard.title}
                    </SettingTitle>
                </SpinBoxRow>
            </Item>
        {/if}

        <Item>
            <EnumSelectorRow
                bind:value={$config.interdayLearningMix}
                defaultValue={defaults.interdayLearningMix}
                choices={reviewMixChoices()}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("interdayStepPriority"),
                        )}
                >
                    {settings.interdayStepPriority.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        <Item>
            <EnumSelectorRow
                bind:value={intradayLearningMix}
                defaultValue={ReviewMix.MIX_WITH_REVIEWS}
                choices={reviewMixChoices()}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("intradayStepPriority"),
                        )}
                >
                    {settings.intradayStepPriority.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        <Item>
            <EnumSelectorRow
                bind:value={$config.reviewOrder}
                defaultValue={defaults.reviewOrder}
                choices={reviewOrderChoices($fsrs)}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(Object.keys(settings).indexOf("reviewSortOrder"))}
                >
                    {settings.reviewSortOrder.title}
                </SettingTitle>
            </EnumSelectorRow>
        </Item>
    </DynamicallySlottable>
</TitledContainer>
