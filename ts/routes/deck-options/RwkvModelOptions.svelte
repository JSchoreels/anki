<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { withoutUnicodeIsolation } from "@tslib/i18n";

    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";

    import GlobalLabel from "./GlobalLabel.svelte";
    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    export let disabled: boolean = false;

    const settings = state.rwkvProfileSettings;
    const models = [
        { value: "legacy", label: tr.deckConfigRwkvModelLegacy() },
        { value: "sm40", label: tr.deckConfigRwkvModelSm40() },
        { value: "sm36", label: tr.deckConfigRwkvModelSm36() },
        { value: "sm55", label: tr.deckConfigRwkvModelSm55() },
    ];
    const precisions = [
        { value: "f32", label: tr.deckConfigRwkvF32() },
        { value: "int8", label: tr.deckConfigRwkvInt8() },
        { value: "int4", label: tr.deckConfigRwkvInt4() },
        {
            value: "cards_int4_notes_int8",
            label: tr.deckConfigRwkvInt4CardsInt8Notes(),
        },
    ];
</script>

<section aria-label={tr.deckConfigRwkvModelAndStorage()}>
    <h2>{tr.deckConfigRwkvModelAndStorage()}</h2>
    <p class="scope">{tr.deckConfigRwkvProfileSettingsScope()}</p>
    <EnumSelectorRow
        bind:value={$settings.model}
        defaultValue="sm55"
        choices={models}
        {disabled}
    >
        <GlobalLabel title={tr.deckConfigRwkvModel()} />
    </EnumSelectorRow>
    <p>{tr.deckConfigRwkvModelExplanation()}</p>
    <p>
        {@html withoutUnicodeIsolation(
            tr.deckConfigRwkvModelResults({
                factory: "https://jschoreels.github.io/anki-rwkv/factory.html",
                backup: "https://github.com/JSchoreels/anki/blob/main/docs/rwkv-four-model-quality-2026-10-22.MD",
            }),
        )}
    </p>
    <EnumSelectorRow
        bind:value={$settings.statePrecision}
        defaultValue="cards_int4_notes_int8"
        choices={precisions}
        {disabled}
    >
        <GlobalLabel title={tr.deckConfigRwkvPrecision()} />
    </EnumSelectorRow>
    <p>{tr.deckConfigRwkvPrecisionExplanation()}</p>
</section>

<style>
    h2 {
        font-size: 1rem;
        font-weight: 700;
        margin: 0 0 0.5rem;
    }

    p {
        color: var(--fg-subtle);
        font-size: 0.85rem;
        margin: 0.5rem 0;
    }

    .scope {
        margin-bottom: 1rem;
    }
</style>
