<!--
Copyright: Anki Software, LLC and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { stepsToString, stringToSteps } from "./steps";

    export let value: number[];
    export let inlineLabel: string | undefined = undefined;

    let stringValue: string;
    $: stringValue = stepsToString(value);

    function update(this: HTMLInputElement): void {
        value = stringToSteps(this.value);
    }
</script>

<input
    type="text"
    class:study-value={!!inlineLabel}
    aria-label={inlineLabel}
    style:width={inlineLabel ? `${Math.max(4, stringValue.length + 1)}ch` : undefined}
    value={stringValue}
    on:blur={update}
    on:keydown={(e) => {
        if (e.key === "Enter") {
            e.currentTarget.blur();
        }
    }}
/>

<style lang="scss">
    input {
        width: 100%;
    }
</style>
