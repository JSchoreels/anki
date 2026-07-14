<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import Col from "$lib/components/Col.svelte";
    import ConfigInput from "$lib/components/ConfigInput.svelte";
    import RevertButton from "$lib/components/RevertButton.svelte";
    import Row from "$lib/components/Row.svelte";
    import SpinBox from "$lib/components/SpinBox.svelte";

    import {
        RWKV_REVIEW_BATCH_SIZE_MAX,
        RWKV_REVIEW_BATCH_SIZE_MIN,
        rwkvEstimatedMemoryLabel,
    } from "./rwkv-batch-size";

    export let value: number;
    export let defaultValue: number;
</script>

<Row --cols={13}>
    <Col --col-size={7} breakpoint="xs">
        <slot />
    </Col>
    <Col --col-size={6} breakpoint="xs">
        <Row class="flex-grow-1">
            <ConfigInput>
                <div class="rwkv-batch-size-control">
                    <SpinBox
                        bind:value
                        min={RWKV_REVIEW_BATCH_SIZE_MIN}
                        max={RWKV_REVIEW_BATCH_SIZE_MAX}
                        step={1}
                    />
                    <span class="memory-estimate">
                        {tr.deckConfigRwkvReviewBatchSizeMemory({
                            memory: rwkvEstimatedMemoryLabel(value),
                        })}
                    </span>
                </div>
                <RevertButton slot="revert" bind:value {defaultValue} />
            </ConfigInput>
        </Row>
    </Col>
</Row>

<style lang="scss">
    .rwkv-batch-size-control {
        width: 100%;
        display: grid;
        grid-template-columns: minmax(5rem, 1fr) auto;
        align-items: center;
        gap: 0.75rem;
    }

    .memory-estimate {
        color: var(--fg-subtle);
        font-size: 0.9rem;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
    }
</style>
