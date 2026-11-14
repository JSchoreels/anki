<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { withoutUnicodeIsolation } from "@tslib/i18n";

    import Switch from "$lib/components/Switch.svelte";

    import type { DeckOptionsState } from "./lib";
    import {
        RWKV_MODEL_BIG,
        RWKV_MODEL_SMALL,
        RWKV_MODEL_TINY,
        RWKV_OLDER_SMALL_MODELS,
    } from "./lib";

    export let state: DeckOptionsState;
    export let disabled: boolean = false;

    const settings = state.rwkvProfileSettings;

    // The three sizes of the RWKV family release, stored under the profile's
    // model key: each runs only its own two models, a two-head model for
    // RWKV-Curve (and both schedulers under the opt-out) and an Instant-Only
    // model for RWKV-Instant (qt/aqt/rwkv_model.py).
    const sizes = [
        {
            value: RWKV_MODEL_BIG,
            label: tr.deckConfigRwkvModelBig(),
            description: tr.deckConfigRwkvModelBigDescription(),
        },
        {
            value: RWKV_MODEL_SMALL,
            label: tr.deckConfigRwkvModelSmall(),
            description: tr.deckConfigRwkvModelSmallDescription(),
        },
        {
            value: RWKV_MODEL_TINY,
            label: tr.deckConfigRwkvModelTiny(),
            description: tr.deckConfigRwkvModelTinyDescription(),
        },
    ];
    // The show-time retrain of the release (SM92) stays a preserved
    // advanced value: listed only for a profile that already holds it.
    const keptChoice = {
        value: "sm92",
        label: tr.deckConfigRwkvModelSm92(),
        description: tr.deckConfigRwkvModelSm92Description(),
    };

    // Older small models switch to Small on save, so they show as Small.
    $: selected = RWKV_OLDER_SMALL_MODELS.includes($settings.model)
        ? RWKV_MODEL_SMALL
        : $settings.model;
    $: choices = selected === keptChoice.value ? [...sizes, keptChoice] : sizes;
    // Every size runs RWKV-Instant on its own Instant-Only model: beside its
    // two-head model when presets use both RWKV-Instant and RWKV-Curve, alone
    // otherwise. The opt-out only matters for both; its description says so,
    // since the runtime follows every preset's scheduler at profile open,
    // not just this one's.
    $: hasInstantModel =
        selected === RWKV_MODEL_BIG ||
        selected === RWKV_MODEL_SMALL ||
        selected === RWKV_MODEL_TINY;

    function changeSingleModel(event: Event): void {
        $settings.singleModel = (event.target as HTMLInputElement).checked;
    }

    function changeModel(value: string): void {
        $settings.model = value;
    }
</script>

<section aria-label={tr.deckConfigRwkvModel()}>
    <div class="model-sizes" role="radiogroup" aria-label={tr.deckConfigRwkvModel()}>
        {#each choices as choice (choice.value)}
            <label class="model-size">
                <input
                    type="radio"
                    name="rwkv-model-size"
                    value={choice.value}
                    checked={selected === choice.value}
                    {disabled}
                    aria-labelledby="rwkv-model-size-{choice.value}-label"
                    aria-describedby="rwkv-model-size-{choice.value}"
                    on:change={() => changeModel(choice.value)}
                />
                <span class="size-label" id="rwkv-model-size-{choice.value}-label">
                    {choice.label}
                </span>
                <span class="size-description" id="rwkv-model-size-{choice.value}">
                    {choice.description}
                </span>
            </label>
        {/each}
    </div>
    <div class="model-description">
        <p>{tr.deckConfigRwkvModelComparison()}</p>
        {#if hasInstantModel}
            <div class="model-row">
                <label class="single-model-label" for="rwkv-single-model">
                    {tr.deckConfigRwkvSingleModel()}
                </label>
                <Switch
                    id="rwkv-single-model"
                    value={$settings.singleModel}
                    {disabled}
                    on:input={changeSingleModel}
                />
            </div>
            <p>{tr.deckConfigRwkvSingleModelExplanation()}</p>
            <p>{tr.deckConfigRwkvSingleModelInstantOnly()}</p>
        {/if}
        <p>{tr.deckConfigRwkvProfileSettingsScope()}</p>
        <p>{tr.deckConfigRwkvModelSizeMigration()}</p>
        <p>
            {@html withoutUnicodeIsolation(
                tr.deckConfigRwkvModelResults({
                    factory: "https://jschoreels.github.io/anki-rwkv/factory.html",
                    backup: "https://github.com/JSchoreels/anki/blob/main/docs/rwkv-sm56-2026-10-24.MD#matched-native-backup-check",
                }),
            )}
        </p>
    </div>
</section>

<style>
    .model-sizes {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }

    .model-size {
        align-items: baseline;
        cursor: pointer;
        display: grid;
        gap: 0 0.5rem;
        grid-template-columns: auto 1fr;
        margin: 0;
    }

    .model-size input {
        grid-column: 1;
        grid-row: 1 / span 2;
        margin: 0;
    }

    .size-label {
        font-weight: 600;
        grid-column: 2;
    }

    .size-description {
        color: var(--fg-subtle);
        font-size: 0.8125rem;
        grid-column: 2;
        line-height: 1.5;
    }

    .model-row {
        align-items: center;
        display: flex;
        gap: 0.75rem;
        min-height: 2.5rem;
    }

    .single-model-label {
        flex: 1;
        margin: 0;
    }

    .model-description {
        color: var(--fg-subtle);
        font-size: 0.8125rem;
        line-height: 1.5;
        margin-top: 0.25rem;
    }

    p {
        margin: 0.375rem 0 0;
    }
</style>
