<!--
Copyright: Anki Software, LLC and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    export let value: number;
    export let label: string;
    export let min = 0;
    export let max = 9999;
    export let step = 1;
    export let scale = 1;
    export let focused = false;

    function update(input: HTMLInputElement) {
        if (input.value !== "" && input.validity.valid) {
            value = input.valueAsNumber / scale;
        }
    }
    function finish(input: HTMLInputElement) {
        if (input.value !== "" && Number.isFinite(input.valueAsNumber)) {
            const snapped = min + Math.round((input.valueAsNumber - min) / step) * step;
            value = Math.max(min, Math.min(max, Number(snapped.toFixed(4)))) / scale;
        }
        input.value = String(Number((value * scale).toFixed(4)));
    }
</script>

<input
    class="study-value"
    type="number"
    aria-label={label}
    {min}
    {max}
    {step}
    value={Number((value * scale).toFixed(4))}
    style:width={`${Math.max(2, String(Math.round(value * scale * 10) / 10).length + 0.6)}ch`}
    on:focus={() => (focused = true)}
    on:input={(e) => update(e.currentTarget)}
    on:blur={(e) => {
        finish(e.currentTarget);
        focused = false;
    }}
    on:keydown={(e) => {
        if (e.key === "Enter") {
            e.currentTarget.blur();
        }
    }}
/>
