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

    const settings = {
        fsrs: {
            title: tr.deckConfigSchedulerEnableFsrs(),
            help: tr.deckConfigFsrsTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            global: true,
        },
        desiredRetention: {
            title: tr.deckConfigDesiredRetention(),
            help:
                tr.deckConfigDesiredRetentionTooltip() +
                "\n\n" +
                tr.deckConfigDesiredRetentionTooltip2(),
            sched: HelpItemScheduler.FSRS,
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
    {helpSections}
    on:mount={(e) => {
        modal = e.detail.modal;
        carousel = e.detail.carousel;
    }}
/>
