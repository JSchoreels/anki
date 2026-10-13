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
    export let openHelp: (key: string) => void;

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

<div class="scheduler-options">
    <Item>
        <SchedulerSelect
            id="review-scheduler"
            title={tr.deckConfigSchedulerReview()}
            recommended="RWKV-Instant"
            value={scheduler($config)}
            choices={schedulerChoices}
            onChange={selectScheduler}
            onHelp={() => openHelp("review")}
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
                onHelp={() => openHelp("dueDates")}
            />
        </Item>
    {/if}
</div>

<style>
    .scheduler-options {
        container: scheduler-controls / inline-size;
    }
</style>
