<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import type { Choice } from "$lib/components/EnumSelector.svelte";
    import Item from "$lib/components/Item.svelte";

    import type { DeckOptionsState } from "./lib";
    import {
        dueDateScheduler,
        type DueDateScheduler,
        scheduler,
        type Scheduler,
        withDueDateScheduler,
        withScheduler,
    } from "./scheduler-choice";
    import SchedulerSelect from "./SchedulerSelect.svelte";

    export let state: DeckOptionsState;
    export let onSelect: () => void;

    const config = state.currentConfig;
    const dueDateChoices: Choice<DueDateScheduler>[] = [
        { value: "fsrs-6", label: "FSRS-6" },
        { value: "fsrs-7", label: "FSRS-7" },
        { value: "rwkv-curve", label: "RWKV-Curve" },
    ];
    const schedulerChoices: Choice<Scheduler>[] = [
        ...dueDateChoices,
        { value: "rwkv-instant", label: "RWKV-Instant" },
    ];

    function selectScheduler(choice: Scheduler): void {
        config.update((current) => withScheduler(current, choice));
        onSelect();
    }

    function selectDueDates(choice: DueDateScheduler): void {
        config.update((current) => withDueDateScheduler(current, choice));
        onSelect();
    }
</script>

<div class="scheduler-introduction">
    <p class="scheduler-description">
        {tr.deckConfigRwkvDescription()}
        <a href="https://github.com/JSchoreels/anki/blob/main/RWKV_FAQ.md">
            {tr.deckConfigRwkvReadMore()}
        </a>
    </p>
    <p class="scheduler-description">
        {tr.deckConfigFsrsDescription()}
        <a href="https://github.com/open-spaced-repetition/srs-benchmark">
            {tr.deckConfigFsrsReadMore()}
        </a>
    </p>
</div>

<Item>
    <SchedulerSelect
        id="review-scheduler"
        title={tr.deckConfigSchedulerReview()}
        recommended="RWKV-Instant"
        value={scheduler($config)}
        choices={schedulerChoices}
        onChange={selectScheduler}
    />
</Item>

{#if $config.rwkvReviewInstantOrderEnabled}
    <Item>
        <SchedulerSelect
            id="due-date-scheduler"
            title={tr.deckConfigSchedulerDueDates()}
            recommended="FSRS-7"
            value={dueDateScheduler($config)}
            choices={dueDateChoices}
            onChange={selectDueDates}
        />
        <p class="scheduler-description">
            {tr.deckConfigSchedulerDueDatesDescription()}
        </p>
    </Item>
{/if}

<style>
    .scheduler-introduction {
        margin: 0.5rem 0 0.875rem;
    }

    .scheduler-description {
        color: var(--fg-subtle);
        font-size: 0.8rem;
        font-style: italic;
        margin: 0.375rem 0 0;
    }
</style>
