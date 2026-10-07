<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import Col from "$lib/components/Col.svelte";
    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import type { Choice } from "$lib/components/EnumSelector.svelte";
    import Row from "$lib/components/Row.svelte";

    type T = $$Generic;
    export let id: string;
    export let title: string;
    export let value: T;
    export let choices: Choice<T>[];
    export let legacyChoices: Choice<T>[];
    export let onChange: (value: T) => void;

    let previousValue = value;
    let legacy = legacyChoices.some((choice) => choice.value === value);
    $: if (previousValue !== value) {
        previousValue = value;
        legacy = legacyChoices.some((choice) => choice.value === value);
    }
    $: visibleChoices = legacy ? legacyChoices : choices;
</script>

<Row --cols={13}>
    <Col --col-size={5} breakpoint="md">
        <span id={`${id}-label`}>{title}</span>
    </Col>
    <Col --col-size={8} breakpoint="md">
        <ConfigInput>
            <div class="scheduler-tabs" role="group" aria-label={title}>
                <button
                    class:active={!legacy}
                    aria-pressed={!legacy}
                    on:click={() => (legacy = false)}
                >
                    {tr.deckConfigSchedulerModern()}
                </button>
                <button
                    class:active={legacy}
                    aria-pressed={legacy}
                    on:click={() => (legacy = true)}
                >
                    {tr.deckConfigSchedulerLegacy()}
                </button>
            </div>
            <div
                class="scheduler-choices"
                role="radiogroup"
                aria-labelledby={`${id}-label`}
            >
                {#each visibleChoices as choice}
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
    </Col>
</Row>

<style>
    .scheduler-tabs {
        display: flex;
        justify-content: space-around;
        margin-bottom: 0.375rem;
    }

    .scheduler-tabs button {
        background: transparent;
        border: 0;
        border-bottom: 3px solid transparent;
        border-radius: 0;
        box-shadow: none;
        color: var(--fg-subtle);
        font-size: 0.8rem;
        padding: 0.125rem 0.375rem;
    }

    .scheduler-tabs button:hover,
    .scheduler-tabs button.active {
        color: var(--fg);
    }

    .scheduler-tabs button.active {
        border-bottom-color: var(--border-focus);
    }

    .scheduler-choices {
        display: flex;
        flex-wrap: wrap;
        gap: 0.125rem;
        padding: 0.1875rem;
        background: var(--canvas-inset);
        border: 1px solid var(--border);
        border-radius: 0.5rem;
    }

    .scheduler-choices label {
        position: relative;
        flex: 1 0 auto;
        margin: 0;
        padding: 0.375rem 0.5rem;
        border-radius: 0.3125rem;
        color: var(--fg-subtle);
        cursor: pointer;
        font-size: 0.8rem;
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
        background: var(--border-focus);
        color: white;
    }

    .scheduler-choices label:focus-within {
        outline: 2px solid var(--border-focus);
        outline-offset: 2px;
    }
</style>
