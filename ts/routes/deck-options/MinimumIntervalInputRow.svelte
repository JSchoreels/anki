<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import Col from "$lib/components/Col.svelte";
    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import RevertButton from "$lib/components/RevertButton.svelte";
    import Row from "$lib/components/Row.svelte";

    import {
        minimumIntervalToString,
        stringToMinimumInterval,
    } from "./minimum-interval";

    export let value: number;
    export let defaultValue: number;

    let inputValue = minimumIntervalToString(value);
    $: inputValue = minimumIntervalToString(value);

    function update(): void {
        const parsed = stringToMinimumInterval(inputValue);
        if (parsed !== undefined) {
            value = parsed;
        }
        inputValue = minimumIntervalToString(value);
    }
</script>

<Row --cols={13}>
    <Col --col-size={7} breakpoint="xs">
        <slot />
    </Col>
    <Col --col-size={6} breakpoint="xs">
        <Row class="flex-grow-1">
            <ConfigInput>
                <input type="text" bind:value={inputValue} on:blur={update} />
                <RevertButton slot="revert" bind:value {defaultValue} />
            </ConfigInput>
        </Row>
    </Col>
</Row>

<style>
    input {
        width: 100%;
        -webkit-appearance: none;
        appearance: none;
    }
</style>
