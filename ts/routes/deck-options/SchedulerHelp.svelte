<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import HelpModal from "$lib/components/HelpModal.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    export let title: string;
    export let fsrs: boolean;
    export let showBadge = true;

    const modelIntroduction = [
        "### RWKV",
        tr.deckConfigRwkvDescription(),
        "### FSRS",
        tr.deckConfigFsrsDescription(),
    ].join("\n\n");
    const modelReferences = [
        {
            label: tr.deckConfigRwkvReadMore(),
            url: "https://github.com/JSchoreels/anki/blob/main/RWKV_FAQ.md",
        },
        {
            label: tr.deckConfigFsrsReadMore(),
            url: "https://github.com/open-spaced-repetition/srs-benchmark",
        },
    ];

    const settings = {
        fsrs: {
            title: tr.deckConfigSchedulerEnableFsrs(),
            help: tr.deckConfigFsrsTooltip() + "\n\n" + modelIntroduction,
            references: modelReferences,
            url: HelpPage.DeckOptions.fsrs,
            global: true,
        },
        review: {
            title: tr.deckConfigSchedulerReview(),
            help: modelIntroduction,
            references: modelReferences,
            url: HelpPage.DeckOptions.fsrs,
        },
        dueDates: {
            title: tr.deckConfigSchedulerDueDates(),
            help: tr.deckConfigSchedulerDueDatesDescription(),
        },
        desiredRetention: {
            title: tr.deckConfigDesiredRetention(),
            help:
                tr.deckConfigDesiredRetentionTooltip() +
                "\n\n" +
                tr.deckConfigDesiredRetentionTooltip2(),
        },
        modelParams: {
            title: tr.deckConfigWeights(),
            help:
                tr.deckConfigWeightsTooltip2() +
                "\n\n" +
                tr.deckConfigComputeOptimalWeightsTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
        rescheduleCardsOnChange: {
            title: tr.deckConfigRescheduleCardsOnChange(),
            help: tr.deckConfigRescheduleCardsOnChangeTooltip(),
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
        healthCheck: {
            title: tr.deckConfigHealthCheck(),
            help:
                tr.deckConfigAffectsEntireCollection() +
                "\n\n" +
                tr.deckConfigHealthCheckTooltip1() +
                "\n\n" +
                tr.deckConfigHealthCheckTooltip2(),
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);
    let modal: Modal;
    let carousel: Carousel;

    export function open(key: string): void {
        modal.show();
        carousel.to(Object.keys(settings).indexOf(key));
    }
</script>

<HelpModal
    {title}
    url={HelpPage.DeckOptions.fsrs}
    {fsrs}
    {showBadge}
    {helpSections}
    on:mount={(e) => {
        modal = e.detail.modal;
        carousel = e.detail.carousel;
    }}
/>
