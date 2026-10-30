<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { withoutUnicodeIsolation } from "@tslib/i18n";

    import Switch from "$lib/components/Switch.svelte";

    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    export let disabled: boolean = false;

    const settings = state.rwkvProfileSettings;
    $: useSmallerModel = $settings.model !== "legacy";

    function changeModel(event: Event): void {
        $settings.model = (event.target as HTMLInputElement).checked
            ? "sm72e25"
            : "legacy";
    }
</script>

<section aria-label={tr.deckConfigRwkvModel()}>
    <div class="model-row">
        <label for="rwkv-smaller-model">{tr.deckConfigRwkvUseSmallerModel()}</label>
        <Switch
            id="rwkv-smaller-model"
            value={useSmallerModel}
            {disabled}
            on:input={changeModel}
        />
    </div>
    <div class="model-description">
        <dl>
            <div class="model-choice">
                <dt>{tr.deckConfigRwkvModelOn()}</dt>
                <dd>{tr.deckConfigRwkvModelSmallerDescription()}</dd>
            </div>
            <div class="model-choice">
                <dt>{tr.deckConfigRwkvModelOff()}</dt>
                <dd>{tr.deckConfigRwkvModelBiggerDescription()}</dd>
            </div>
        </dl>
        <p>{tr.deckConfigRwkvProfileSettingsScope()}</p>
        <p>{tr.deckConfigRwkvModelMigration()}</p>
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

    .model-description {
        color: var(--fg-subtle);
        font-size: 0.8125rem;
        line-height: 1.5;
        margin-top: 0.25rem;
    }

    p {
        margin: 0.375rem 0 0;
    }

    dl {
        margin: 0;
    }

    .model-choice {
        display: flex;
        flex-wrap: wrap;
        gap: 0 0.375rem;
    }

    dt {
        font-weight: 600;
    }

    dd {
        margin: 0;
    }
</style>
