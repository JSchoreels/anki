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

    import {
        RWKV_REVIEW_BATCH_SIZE_OPTIONS,
        rwkvBatchSizeForSliderIndex,
        rwkvBatchSizeOption,
        rwkvBatchSizeSliderIndex,
        rwkvEstimatedMemoryLabel,
    } from "./rwkv-batch-size";

    export let value: number;
    export let defaultValue: number;

    $: sliderIndex = rwkvBatchSizeSliderIndex(value);
    $: selectedOption = rwkvBatchSizeOption(value);

    function updateBatchSize(event: Event): void {
        const input = event.currentTarget as HTMLInputElement;
        value = rwkvBatchSizeForSliderIndex(Number(input.value));
    }
</script>

<Row --cols={13}>
    <Col --col-size={7} breakpoint="xs">
        <slot />
    </Col>
    <Col --col-size={6} breakpoint="xs">
        <Row class="flex-grow-1">
            <ConfigInput>
                <div class="rwkv-batch-size-control">
                    <div class="selected-batch">
                        <span>
                            {tr.deckConfigRwkvReviewBatchSizeCurrent({
                                cards: selectedOption.size,
                            })}
                            {#if selectedOption.recommended}
                                ({tr.deckConfigRwkvReviewBatchSizeRecommended()})
                            {/if}
                        </span>
                        <span>
                            {tr.deckConfigRwkvReviewBatchSizeMemory({
                                memory: rwkvEstimatedMemoryLabel(selectedOption.size),
                            })}
                        </span>
                    </div>
                    <input
                        type="range"
                        min="0"
                        max={RWKV_REVIEW_BATCH_SIZE_OPTIONS.length - 1}
                        step="1"
                        value={sliderIndex}
                        aria-label={tr.deckConfigRwkvReviewBatchSize()}
                        on:input={updateBatchSize}
                    />
                    <div class="batch-options" aria-hidden="true">
                        {#each RWKV_REVIEW_BATCH_SIZE_OPTIONS as option}
                            <span class:selected={option.size === selectedOption.size}>
                                {option.size}
                            </span>
                        {/each}
                    </div>
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
        gap: 0.35rem;

        input[type="range"] {
            width: 100%;
            cursor: pointer;
        }
    }

    .selected-batch {
        display: flex;
        flex-wrap: wrap;
        justify-content: space-between;
        gap: 0.75rem;
        font-size: 0.9rem;
        line-height: 1.2;
    }

    .batch-options {
        display: grid;
        grid-template-columns: repeat(6, minmax(0, 1fr));
        font-size: 0.75rem;
        line-height: 1.15;
        color: var(--fg-subtle);
        text-align: center;
        font-variant-numeric: tabular-nums;

        & > span {
            min-inline-size: 0;
        }

        .selected {
            color: var(--fg);
            font-weight: 600;
        }
    }
</style>
