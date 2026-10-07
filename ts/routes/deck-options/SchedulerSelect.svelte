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
    export let recommended: string;
    export let value: T;
    export let choices: Choice<T>[];
    export let onChange: (value: T) => void;
</script>

<Row --cols={13}>
    <Col --col-size={5} breakpoint="md">
        <div>
            <span id={`${id}-label`}>{title}</span>
            <p class="scheduler-recommendation" id={`${id}-recommendation`}>
                <span class="recommendation-star" aria-hidden="true">★</span>
                {tr.deckConfigSchedulerRecommended({ scheduler: recommended })}
            </p>
        </div>
    </Col>
    <Col --col-size={8} breakpoint="md">
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
    </Col>
</Row>

<style>
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
