<!--
Copyright: Anki Software, LLC and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import Item from "$lib/components/Item.svelte";
    import Row from "$lib/components/Row.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";

    import ApplyScheduleModal from "./ApplyScheduleModal.svelte";
    import FsrsOptions from "./FsrsOptions.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import { type DeckOptionsState, ValueTab } from "./lib";
    import MaximumIntervalInputRow from "./MaximumIntervalInputRow.svelte";
    import SchedulerHelp from "./SchedulerHelp.svelte";
    import SchedulerOptions from "./SchedulerOptions.svelte";
    import SchedulerHelpButton from "./SchedulerHelpButton.svelte";
    import SchedulerRetention from "./SchedulerRetention.svelte";
    import { schedulerCardOrder } from "./scheduler-choice";
    import Warning from "./Warning.svelte";

    export let state: DeckOptionsState;
    export let study = false;
    export let api: Record<string, never>;

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

    const fsrs = state.fsrs;
    const config = state.currentConfig;
    const defaults = state.defaults;
    const limits = state.deckLimits;
    let schedulerHelp: SchedulerHelp;
    let desiredRetentionFocused = false;
    let desiredRetentionWarning = "";
    let retentionWarningClass = "";
    let effectiveDesiredRetention =
        $limits.desiredRetention ?? $config.desiredRetention;
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
    let newlyEnabled = false;
    $: modelCards = schedulerCardOrder($config);
    $: if (!$fsrs) {
        newlyEnabled = true;
    }
</script>

<Row class="row-columns">
    <TitledContainer title={study ? tr.deckConfigStudyScheduling() : "Scheduler"}>
        <ApplyScheduleModal slot="header-actions" {state} />
        <SchedulerHelp
            title={"Scheduler"}
            showBadge={false}
            fsrs={$fsrs}
            bind:this={schedulerHelp}
        />
        <DynamicallySlottable slotHost={Item} {api}>
            <Item>
                <SwitchRow bind:value={$fsrs} defaultValue={false}>
                    <span class="scheduler-setting-label">
                        <GlobalLabel title={tr.deckConfigSchedulerEnableFsrs()} />
                        <SchedulerHelpButton
                            title={tr.deckConfigSchedulerEnableFsrs()}
                            on:click={() => schedulerHelp.open("fsrs")}
                        />
                    </span>
                </SwitchRow>
            </Item>

            {#if !$fsrs}
                <p class="scheduler-disabled">{tr.deckConfigSchedulerDisabled()}</p>
            {/if}

            <SchedulerOptions
                {state}
                {study}
                onSelect={() => fsrs.set(true)}
                openHelp={(key) => schedulerHelp.open(key)}
            />

            <div
                class:study-line={study}
                class:combined-values={study}
                class:pass-through={!study}
            >
                {#if $fsrs || $config.rwkvReviewEnabled}
                    <Item>
                        <SchedulerRetention
                            {study}
                            bind:value={effectiveDesiredRetention}
                            defaultValue={defaults.desiredRetention}
                            tabs={desiredRetentionTabs}
                            bind:focused={desiredRetentionFocused}
                        >
                            <span>{tr.deckConfigDesiredRetention()}</span>
                            <SchedulerHelpButton
                                title={tr.deckConfigDesiredRetention()}
                                on:click={() => schedulerHelp.open("desiredRetention")}
                            />
                        </SchedulerRetention>
                    </Item>
                {/if}
                {#if study}
                    <Item>
                        <span>
                            {tr.schedulingMaximumInterval()}
                            <MaximumIntervalInputRow
                                inlineLabel={tr.schedulingMaximumInterval()}
                                defaultValue={defaults.maximumReviewInterval}
                                bind:value={$config.maximumReviewInterval}
                            />
                        </span>
                    </Item>
                {/if}
            </div>
            {#if $fsrs || $config.rwkvReviewEnabled}
                <Warning
                    warning={desiredRetentionWarning}
                    className={retentionWarningClass}
                />
            {/if}
            {#if study && $config.maximumReviewInterval < 180}
                <Warning
                    warning={tr.deckConfigTooShortMaximumInterval()}
                    className={$config.maximumReviewInterval < 50
                        ? "alert-danger"
                        : "alert-warning"}
                />
            {/if}
        </DynamicallySlottable>
    </TitledContainer>
</Row>

{#each modelCards as card (card)}
    {#if card === "rwkv"}
        <slot name="rwkv" />
    {:else if card === "fsrs" && ($fsrs || $config.rwkvReviewEnabled)}
        <!-- The simulator's embedded controls also need a slot-host context. -->
        <DynamicallySlottable slotHost={Item} api={{}}>
            <FsrsOptions
                {state}
                {study}
                {newlyEnabled}
                {desiredRetentionTabs}
                bind:effectiveDesiredRetention
                bind:desiredRetentionFocused
                bind:desiredRetentionWarning
                bind:retentionWarningClass
                openSchedulerHelp={(key) => schedulerHelp.open(key)}
                {onPresetChange}
            />
        </DynamicallySlottable>
    {/if}
{/each}

<style>
    .pass-through {
        display: contents;
    }
    .combined-values {
        align-items: baseline;
    }
    .scheduler-setting-label {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
    }

    .scheduler-disabled {
        color: var(--fg-subtle);
        font-size: 0.8rem;
        margin: 0.375rem 0;
    }
</style>
