<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import RevertButton from "$lib/components/RevertButton.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SpinBox from "$lib/components/SpinBox.svelte";

    export let reviews: number;
    export let seconds: number;
    export let defaultReviews: number;
    export let defaultSeconds: number;
    export let onHelp: () => void;

    const reviewsMarker = "__RWKV_REVIEWS__";
    const secondsMarker = "__RWKV_SECONDS__";
    const parts = tr
        .deckConfigRwkvRepeatSpacing({
            reviewsField: reviewsMarker,
            secondsField: secondsMarker,
        })
        .split(/(__RWKV_REVIEWS__|__RWKV_SECONDS__)/);
</script>

<fieldset class="repeat-spacing">
    <legend class="visually-hidden">{tr.deckConfigRwkvRepeatSpacingTitle()}</legend>
    {#each parts as part}
        {#if part === reviewsMarker}
            <div class="repeat-value">
                <label class="repeat-input">
                    <span class="visually-hidden">
                        {tr.deckConfigRwkvReviewMinInterveningReviews()}
                    </span>
                    <SpinBox bind:value={reviews} min={0} max={10000} />
                    <span class="repeat-unit" aria-hidden="true">
                        {tr.deckConfigRwkvRepeatReviewsUnit({ count: reviews })}
                    </span>
                </label>
                <RevertButton bind:value={reviews} defaultValue={defaultReviews} />
            </div>
        {:else if part === secondsMarker}
            <div class="repeat-value">
                <label class="repeat-input">
                    <span class="visually-hidden">
                        {tr.deckConfigRwkvReviewMinElapsedSecs()}
                    </span>
                    <SpinBox bind:value={seconds} min={0} max={86400} />
                    <span class="repeat-unit" aria-hidden="true">
                        {tr.deckConfigRwkvRepeatSecondsUnit({ count: seconds })}
                    </span>
                </label>
                <RevertButton bind:value={seconds} defaultValue={defaultSeconds} />
            </div>
        {:else if part.trim()}
            <SettingTitle on:click={onHelp}>{part.trim()}</SettingTitle>
        {/if}
    {/each}
</fieldset>

<style>
    .repeat-spacing {
        align-items: center;
        border: 0;
        display: flex;
        flex-wrap: wrap;
        font-size: 0.875rem;
        gap: 0.375rem;
        margin: 0.5rem 0;
        min-inline-size: 0;
        padding: 0;
    }

    .repeat-value {
        --buttons-size: 14px;
        align-items: center;
        color: var(--fg-faint);
        display: inline-flex;
        gap: 0.125rem;
    }

    .repeat-value:hover,
    .repeat-value:focus-within {
        color: var(--fg-subtle);
    }

    .repeat-input {
        align-items: center;
        background: var(--canvas-inset);
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        color: var(--fg);
        display: flex;
        gap: 0.25rem;
        padding-inline-end: 0.5rem;
    }

    .repeat-input:focus-within {
        border-color: var(--border-focus);
    }

    .repeat-input :global(.spin-box) {
        background: transparent;
        border: 0;
        width: 5rem;
    }

    .repeat-input :global(input) {
        min-width: 0;
        width: 0;
    }

    .repeat-unit {
        color: var(--fg-subtle);
        font-size: 0.75rem;
        white-space: nowrap;
    }
</style>
