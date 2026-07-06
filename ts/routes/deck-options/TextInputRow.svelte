<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import Col from "$lib/components/Col.svelte";
    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import RevertButton from "$lib/components/RevertButton.svelte";
    import Row from "$lib/components/Row.svelte";

    export let value: string;
    export let defaultValue: string;
</script>

<Row --cols={13}>
    <Col --col-size={7} breakpoint="xs">
        <slot />
    </Col>
    <Col --col-size={6} breakpoint="xs">
        <Row class="flex-grow-1">
            <ConfigInput>
                <input type="text" bind:value class="form-control" />
                <RevertButton slot="revert" bind:value {defaultValue} />
            </ConfigInput>
        </Row>
    </Col>
</Row>
