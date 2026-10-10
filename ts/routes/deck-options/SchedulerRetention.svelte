<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import RevertButton from "$lib/components/RevertButton.svelte";
    import SpinBox from "$lib/components/SpinBox.svelte";

    import type { ValueTab } from "./lib";
    import TabbedValue from "./TabbedValue.svelte";

    export let value: number;
    export let defaultValue: number;
    export let focused: boolean;
    export let tabs: ValueTab[];
</script>

<div class="scheduler-retention">
    <div class="retention-label"><slot /></div>
    <div class="retention-controls">
        <div class="retention-input">
            <ConfigInput grow={false}>
                <SpinBox
                    bind:value
                    min={0.1}
                    max={0.99}
                    step={0.01}
                    percentage
                    bind:focused
                />
                <RevertButton slot="revert" bind:value {defaultValue} />
            </ConfigInput>
        </div>
        <TabbedValue {tabs} bind:value compact />
    </div>
</div>

<style>
    .scheduler-retention {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 0.75rem;
        border-top: 1px solid var(--border);
        margin-top: 1.25rem;
        padding-top: 1rem;
        padding-bottom: 0.25rem;
    }

    .retention-label,
    .retention-controls {
        display: flex;
        align-items: center;
        gap: 0.375rem;
    }

    .retention-controls {
        gap: 0.75rem;
        flex-wrap: wrap;
    }

    .retention-input {
        width: 5rem;
        margin-inline-end: 1rem;
        font-size: 0.85rem;
    }

    .retention-input :global(input) {
        min-width: 0;
        width: 100%;
    }

    @media (max-width: 575px) {
        .retention-controls {
            width: 100%;
            justify-content: space-between;
        }
    }
</style>
