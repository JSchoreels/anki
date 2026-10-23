<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { withoutUnicodeIsolation } from "@tslib/i18n";

    import Icon from "$lib/components/Icon.svelte";
    import { infoCircle } from "$lib/components/icons";
    import Switch from "$lib/components/Switch.svelte";

    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    export let disabled: boolean = false;

    const settings = state.rwkvProfileSettings;
    let showModelHelp = false;
    $: useBiggerModel = $settings.model === "legacy";

    function changeModel(event: Event): void {
        $settings.model = (event.target as HTMLInputElement).checked
            ? "legacy"
            : "sm55";
    }
</script>

<section aria-label={tr.deckConfigRwkvModel()}>
    <div class="model-row">
        <label for="rwkv-bigger-model">{tr.deckConfigRwkvUseBiggerModel()}</label>
        <Switch
            id="rwkv-bigger-model"
            value={useBiggerModel}
            {disabled}
            on:input={changeModel}
        />
        <button
            type="button"
            class="model-help"
            aria-label={tr.deckConfigRwkvModelHelp()}
            aria-expanded={showModelHelp}
            aria-controls="rwkv-model-manual"
            on:click={() => (showModelHelp = !showModelHelp)}
        >
            <Icon icon={infoCircle} />
        </button>
    </div>
    <p class="model-status" aria-live="polite">
        {useBiggerModel ? tr.deckConfigRwkvModelLegacy() : tr.deckConfigRwkvModelSm55()}
    </p>
    <aside
        id="rwkv-model-manual"
        hidden={!showModelHelp}
        aria-label={tr.deckConfigRwkvModelHelp()}
    >
        <h3>{tr.deckConfigRwkvModel()}</h3>
        <dl>
            <dt>{tr.deckConfigRwkvModelOff()}</dt>
            <dd>{tr.deckConfigRwkvModelSmallerDescription()}</dd>
            <dt>{tr.deckConfigRwkvModelOn()}</dt>
            <dd>{tr.deckConfigRwkvModelBiggerDescription()}</dd>
        </dl>
        <p>{tr.deckConfigRwkvProfileSettingsScope()}</p>
        <p>{tr.deckConfigRwkvModelMigration()}</p>
        <p>
            {@html withoutUnicodeIsolation(
                tr.deckConfigRwkvModelResults({
                    factory: "https://jschoreels.github.io/anki-rwkv/factory.html",
                    backup: "https://github.com/JSchoreels/anki/blob/main/docs/rwkv-four-model-quality-2026-10-22.MD",
                }),
            )}
        </p>
    </aside>
</section>

<style>
    .model-row {
        align-items: center;
        display: flex;
        gap: 0.75rem;
        min-height: 2.5rem;
    }

    label {
        flex: 1;
        margin: 0;
    }

    .model-help {
        background: transparent;
        border: none;
        color: var(--fg);
        flex-shrink: 0;
        height: 1.75rem;
        padding: 0.25rem;
        width: 1.75rem;
    }

    .model-status {
        margin: 0 0 0.5rem;
    }

    aside {
        background: var(--canvas-inset);
        border-radius: 0.5rem;
        font-size: 0.85rem;
        margin-top: 0.75rem;
        padding: 1rem;
    }

    h3 {
        font-size: 0.9rem;
        font-weight: 600;
        margin: 0 0 0.75rem;
    }

    p {
        color: var(--fg-subtle);
        font-size: 0.85rem;
        margin: 0.5rem 0;
    }

    dl {
        margin-bottom: 0.75rem;
    }

    dt {
        font-weight: 600;
    }

    dd {
        color: var(--fg-subtle);
        margin: 0.125rem 0 0.75rem;
    }
</style>
