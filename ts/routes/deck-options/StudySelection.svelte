<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        DeckConfig_Config_NewCardGatherPriority as Gather,
        DeckConfig_Config_SuspendedCardOrder as Group,
    } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import Choice from "$lib/components/EnumSelector.svelte";
    import {
        newGatherPriorityChoices,
        newSortOrderChoices,
        reviewMixChoices,
        suspendedCardOrderChoices,
        disabledSortOrders,
    } from "./choices";
    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    const config = state.currentConfig;
    const pairs = [
        [Gather.LOWEST_POSITION, Gather.HIGHEST_POSITION, tr.deckConfigStudyPosition()],
        [
            Gather.NOTE_FIELD_ASCENDING,
            Gather.NOTE_FIELD_DESCENDING,
            tr.deckConfigNoteField(),
        ],
        [
            Gather.ASCENDING_RETRIEVABILITY,
            Gather.DESCENDING_RETRIEVABILITY,
            tr.deckConfigStudyRetrievability(),
        ],
    ] as const;
    const criteria = newGatherPriorityChoices()
        .filter((c) => !pairs.some((p) => p[1] === c.value))
        .map((c) => ({
            ...c,
            label: pairs.find((p) => p[0] === c.value)?.[2] ?? c.label,
        }));
    const poolChoices = [
        { value: -1, label: tr.deckConfigStudyOnlyNew() },
        ...suspendedCardOrderChoices().map((c) =>
            c.value === Group.MIXED ? { ...c, label: tr.deckConfigStudyMixed() } : c,
        ),
    ];
    $: pair = pairs.find(
        ([up, down]) =>
            up === $config.newCardGatherPriority ||
            down === $config.newCardGatherPriority,
    );
    $: direction = [
        { value: pair?.[0], label: tr.deckConfigStudyAscending() },
        { value: pair?.[1], label: tr.deckConfigStudyDescending() },
    ];
    $: fields = [
        { value: "", label: tr.deckConfigNoteTypeSortField() },
        ...[...new Set([...state.fieldNames, $config.newCardGatherField])]
            .filter(Boolean)
            .map((f) => ({ value: f, label: f })),
    ];
    function setPool(value: number) {
        $config.includeSuspendedCardsInLearnablePool = value !== -1;
        if (value !== -1) {
            $config.suspendedCardOrder = value;
        }
    }
</script>

<div class="study-line">
    <span title={tr.deckConfigIncludeSuspendedCardsInLearnablePoolTooltip()}>
        <Choice
            inlineValue
            accessibleLabel={tr.deckConfigStudyPool()}
            choices={poolChoices}
            value={$config.includeSuspendedCardsInLearnablePool
                ? $config.suspendedCardOrder
                : -1}
            on:change={(e) => setPool(e.detail.value)}
        />
    </span>
    <span>
        {tr.deckConfigStudyBy()}
        <Choice
            inlineValue
            accessibleLabel={tr.deckConfigStudyBy()}
            choices={criteria}
            value={pair?.[0] ?? $config.newCardGatherPriority}
            on:change={(e) => ($config.newCardGatherPriority = e.detail.value)}
        />
        {#if pair?.[0] === Gather.NOTE_FIELD_ASCENDING}
            <Choice
                inlineValue
                accessibleLabel={tr.deckConfigNoteField()}
                choices={fields}
                bind:value={$config.newCardGatherField}
            />
        {/if}
        {#if pair}
            <Choice
                inlineValue
                accessibleLabel={tr.deckConfigStudyDirection()}
                choices={direction}
                value={$config.newCardGatherPriority}
                on:change={(e) => ($config.newCardGatherPriority = e.detail.value)}
            />
        {/if}
    </span>
</div>
{#if pair?.[0] !== Gather.NOTE_FIELD_ASCENDING && pair?.[0] !== Gather.ASCENDING_RETRIEVABILITY}
    <div class="study-line">
        <span>
            {tr.deckConfigStudyShowSelected()}
            <Choice
                inlineValue
                accessibleLabel={tr.deckConfigNewCardSortOrder()}
                choices={newSortOrderChoices(true)}
                disabledChoices={disabledSortOrders($config.newCardGatherPriority)}
                bind:value={$config.newCardSortOrder}
            />
        </span>
    </div>
{/if}
<div class="study-line">
    <span>
        {tr.deckConfigStudyShowNew()}
        <Choice
            inlineValue
            accessibleLabel={tr.deckConfigStudyShowNew()}
            choices={reviewMixChoices()}
            bind:value={$config.newMix}
        />
    </span>
</div>
