<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import type { Choice } from "$lib/components/EnumSelector.svelte";
    import SchedulerHelpButton from "./SchedulerHelpButton.svelte";

    type T = $$Generic;
    export let id: string;
    export let title: string;
    export let recommended: string;
    export let value: T;
    export let choices: Choice<T>[];
    export let onChange: (value: T) => void;
    export let onHelp: () => void;
</script>

<div class="scheduler-select">
    <div>
        <div class="scheduler-label">
            <span id={`${id}-label`}>{title}</span>
            <SchedulerHelpButton {title} on:click={onHelp} />
        </div>
        <p class="scheduler-recommendation" id={`${id}-recommendation`}>
            <span class="recommendation-star" aria-hidden="true">★</span>
            {tr.deckConfigSchedulerRecommended({ scheduler: recommended })}
        </p>
    </div>
    <ConfigInput>
        <div
            class="scheduler-choices"
            role="radiogroup"
            aria-labelledby={`${id}-label`}
            aria-describedby={`${id}-recommendation`}
        >
            {#each choices as choice}
                <label class:active={choice.value === value}>
                    <input
                        type="radio"
                        name={id}
                        value={choice.value}
                        checked={choice.value === value}
                        on:change={() => onChange(choice.value)}
                    />
                    {choice.label}
                </label>
            {/each}
        </div>
    </ConfigInput>
</div>

<style>
    .scheduler-select {
        display: grid;
        grid-template-columns: 5fr 8fr;
        align-items: center;
        gap: 0.625rem;
        padding: 0.5rem 0.25rem;
    }

    @container scheduler-controls (max-width: 620px) {
        .scheduler-select {
            grid-template-columns: 1fr;
            gap: 0.5rem;
        }
    }

    .scheduler-label {
        display: flex;
        align-items: center;
        gap: 0.25rem;
    }

    .scheduler-recommendation {
        margin: 0.125rem 0 0;
        color: var(--fg-subtle);
        font-size: 0.75rem;
    }

    .recommendation-star {
        color: var(--border-focus);
        margin-inline-end: 0.125rem;
    }

    .scheduler-choices {
        display: flex;
        flex-wrap: wrap;
        gap: 0.375rem;
    }

    .scheduler-choices label {
        position: relative;
        flex: 1 0 auto;
        margin: 0;
        padding: 0.375rem 0.5rem;
        border: 1px solid var(--border);
        border-radius: 0.375rem;
        color: var(--fg-subtle);
        cursor: pointer;
        font-size: 0.75rem;
        text-align: center;
        white-space: nowrap;
    }

    .scheduler-choices input {
        position: absolute;
        opacity: 0;
        width: 1px;
        height: 1px;
    }

    .scheduler-choices label:hover {
        background: var(--canvas-elevated);
        color: var(--fg);
    }

    .scheduler-choices label.active {
        background: color-mix(in srgb, var(--border-focus) 12%, var(--canvas-elevated));
        border-color: var(--border-focus);
        color: var(--border-focus);
    }

    .scheduler-choices label:focus-within {
        outline: 2px solid var(--border-focus);
        outline-offset: 2px;
    }
    @media (pointer: coarse) {
        .scheduler-choices label {
            min-height: 44px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
    }
</style>
