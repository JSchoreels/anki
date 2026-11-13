<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { createLocaleNumberFormat, withoutUnicodeIsolation } from "@tslib/i18n";

    import Switch from "$lib/components/Switch.svelte";

    import type { DeckOptionsState } from "./lib";
    import {
        RWKV_MODEL_BIG,
        RWKV_MODEL_SMALL,
        RWKV_MODEL_TINY,
        RWKV_OLDER_SMALL_MODELS,
    } from "./lib";
    import {
        rwkvModelEstimates,
        rwkvModelLayout,
        type RwkvModelSize,
    } from "./rwkv-model-estimates";

    export let state: DeckOptionsState;
    export let disabled: boolean = false;

    const settings = state.rwkvProfileSettings;
    const usage = state.rwkvSchedulerUsage;
    const number = createLocaleNumberFormat({ maximumFractionDigits: 1 });
    const lossNumber = createLocaleNumberFormat({
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    const labels: Record<RwkvModelSize, string> = {
        [RWKV_MODEL_BIG]: tr.deckConfigRwkvModelBig(),
        [RWKV_MODEL_SMALL]: tr.deckConfigRwkvModelSmall(),
        [RWKV_MODEL_TINY]: tr.deckConfigRwkvModelTiny(),
    };
    const layoutLabels = {
        paired: tr.deckConfigRwkvModelBothSeparate(),
        shared: tr.deckConfigRwkvModelBothShared(),
        curve: tr.deckConfigRwkvModelCurveOnly(),
        instant: tr.deckConfigRwkvModelInstantOnly(),
    };

    // Older small models switch to Small on save; an explicitly saved SM92
    // remains selectable until the user chooses a size.
    $: selected = RWKV_OLDER_SMALL_MODELS.includes($settings.model)
        ? RWKV_MODEL_SMALL
        : $settings.model;
    $: hasInstantModel =
        selected === RWKV_MODEL_BIG ||
        selected === RWKV_MODEL_SMALL ||
        selected === RWKV_MODEL_TINY;
    $: layout = rwkvModelLayout($usage, $settings.singleModel);
    $: estimates = rwkvModelEstimates(
        $usage,
        $settings.singleModel,
        $settings.statePrecision,
        state.rwkvCollectionCounts,
    );

    function formatBytes(bytes: number): string {
        if (bytes < 1_000) {
            return `${number.format(bytes)} B`;
        }
        if (bytes < 1_000_000) {
            return `${number.format(bytes / 1_000)} KB`;
        }
        if (bytes < 1_000_000_000) {
            return `${number.format(bytes / 1_000_000)} MB`;
        }
        return `${number.format(bytes / 1_000_000_000)} GB`;
    }

    function formatLoss(value: number | undefined): string {
        if (value === undefined) {
            return tr.deckConfigRwkvModelNotUsed();
        }
        return value === 0
            ? tr.deckConfigRwkvModelBaseline()
            : `+${lossNumber.format(value)}%`;
    }

    function changeSingleModel(event: Event): void {
        $settings.singleModel = (event.target as HTMLInputElement).checked;
    }

    function changeModel(value: string): void {
        $settings.model = value;
    }
</script>

<section aria-label={tr.deckConfigRwkvModel()}>
    <div class="model-heading">
        <h3>{tr.deckConfigRwkvModelSizeHeading()}</h3>
        <p>
            {#if state.rwkvCollectionCounts}
                {tr.deckConfigRwkvModelEstimateSummary({
                    cards: state.rwkvCollectionCounts.cards,
                    notes: state.rwkvCollectionCounts.notes,
                })}
            {:else}
                {tr.deckConfigRwkvModelEstimateNoCounts()}
            {/if}
        </p>
        <p>{layoutLabels[layout]}</p>
    </div>
    <div class="model-sizes" role="radiogroup" aria-label={tr.deckConfigRwkvModel()}>
        {#each estimates as estimate (estimate.size)}
            <label class="model-size" class:selected={selected === estimate.size}>
                <span class="size-header">
                    <input
                        type="radio"
                        name="rwkv-model-size"
                        value={estimate.size}
                        checked={selected === estimate.size}
                        {disabled}
                        aria-labelledby="rwkv-model-size-{estimate.size}-label"
                        aria-describedby="rwkv-model-size-{estimate.size}-details"
                        on:change={() => changeModel(estimate.size)}
                    />
                    <span class="size-label" id="rwkv-model-size-{estimate.size}-label">
                        {labels[estimate.size]}
                    </span>
                    {#if estimate.size === RWKV_MODEL_SMALL}
                        <span class="default-badge">{tr.deckConfigDefaultName()}</span>
                    {/if}
                </span>
                <span class="size-details" id="rwkv-model-size-{estimate.size}-details">
                    <span class="per-card">
                        {tr.deckConfigRwkvModelPerCardNote({
                            size: formatBytes(estimate.perCardAndNote),
                        })}
                    </span>
                    <span class="size-stats">
                        <span class="state-size">
                            {#if estimate.totalStateBytes !== undefined}
                                ≈ {formatBytes(estimate.totalStateBytes)}
                            {:else}
                                {tr.deckConfigRwkvModelStateUnavailable()}
                            {/if}
                        </span>
                        <span class="stat-caption">
                            {tr.deckConfigRwkvModelStateRamDisk()}
                        </span>
                        <span class="stat-caption">
                            {tr.deckConfigRwkvModelLogloss()}
                        </span>
                        <span class="loss-row">
                            <span>{tr.deckConfigRwkvModelCurveLabel()}</span>
                            <span>{formatLoss(estimate.curveLoss)}</span>
                        </span>
                        <span class="loss-row">
                            <span>{tr.deckConfigRwkvModelInstantLabel()}</span>
                            <span>{formatLoss(estimate.instantLoss)}</span>
                        </span>
                        {#if estimate.size === RWKV_MODEL_BIG && estimate.replaySaving !== undefined}
                            <span class="rebuild-baseline">
                                {tr.deckConfigRwkvModelRebuildBaseline()}
                            </span>
                        {:else if estimate.replaySaving !== undefined}
                            <span class="rebuild-saving">
                                {tr.deckConfigRwkvModelRebuildSaving({
                                    percent: estimate.replaySaving,
                                })}
                            </span>
                        {/if}
                    </span>
                </span>
            </label>
        {/each}
        {#if selected === "sm92"}
            <label class="model-size selected kept-choice">
                <span class="size-header">
                    <input
                        type="radio"
                        name="rwkv-model-size"
                        value="sm92"
                        checked
                        {disabled}
                        aria-labelledby="rwkv-model-size-sm92-label"
                        aria-describedby="rwkv-model-size-sm92-details"
                        on:change={() => changeModel("sm92")}
                    />
                    <span class="size-label" id="rwkv-model-size-sm92-label">
                        {tr.deckConfigRwkvModelSm92()}
                    </span>
                </span>
                <span class="per-card" id="rwkv-model-size-sm92-details">
                    {tr.deckConfigRwkvModelSm92Description()}
                </span>
            </label>
        {/if}
    </div>
    <p class="legend">{tr.deckConfigRwkvModelLossLegend()}</p>
    <details class="estimates-help">
        <summary>{tr.deckConfigRwkvModelEstimatesHelp()}</summary>
        <p>{tr.deckConfigRwkvModelEstimatesExplanation()}</p>
        <p>{tr.deckConfigRwkvModelEstimatesLoss()}</p>
        <p>{tr.deckConfigRwkvModelEstimatesRebuild()}</p>
        <p>{tr.deckConfigRwkvSingleModelInstantOnly()}</p>
        <p>{tr.deckConfigRwkvModelSizeMigration()}</p>
        <p>
            {@html withoutUnicodeIsolation(
                tr.deckConfigRwkvModelResults({
                    factory: "https://jschoreels.github.io/anki-rwkv/factory.html",
                    backup: "https://github.com/JSchoreels/anki/blob/main/docs/rwkv-sm56-2026-10-24.MD#matched-native-backup-check",
                }),
            )}
        </p>
    </details>
    {#if hasInstantModel}
        <div class="sharing">
            <div class="model-row">
                <label class="single-model-label" for="rwkv-single-model">
                    {tr.deckConfigRwkvSingleModel()}
                </label>
                <Switch
                    id="rwkv-single-model"
                    value={$settings.singleModel}
                    {disabled}
                    on:input={changeSingleModel}
                />
            </div>
            <p>{tr.deckConfigRwkvSingleModelExplanation()}</p>
        </div>
    {/if}
    <p class="profile-scope">{tr.deckConfigRwkvProfileSettingsScope()}</p>
</section>

<style>
    .model-heading h3 {
        font-size: 0.9375rem;
        font-weight: 600;
        margin: 0;
    }
    .model-heading p,
    .legend,
    .estimates-help,
    .sharing p,
    .profile-scope {
        color: var(--fg-subtle);
        font-size: 0.8125rem;
        line-height: 1.45;
    }
    p {
        margin: 0.35rem 0 0;
    }
    .model-sizes {
        display: grid;
        gap: 0.5rem;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        margin-top: 0.75rem;
    }
    .model-size {
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: 0.4rem;
        cursor: pointer;
        display: block;
        margin: 0;
        min-width: 0;
        padding: 0.75rem;
    }
    .model-size.selected {
        background: color-mix(in srgb, var(--border-focus) 12%, var(--canvas-elevated));
        border-color: var(--border-focus);
    }
    .model-size:focus-within {
        outline: 2px solid var(--border-focus);
        outline-offset: 1px;
    }
    .size-header {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
    }
    .size-header input {
        accent-color: var(--border-focus);
        margin: 0;
    }
    .size-label {
        font-weight: 600;
    }
    .default-badge {
        background: var(--canvas);
        border-radius: 0.2rem;
        color: var(--fg-subtle);
        font-size: 0.6875rem;
        padding: 0.1rem 0.3rem;
    }
    .size-details {
        display: block;
    }
    .per-card {
        color: var(--fg-subtle);
        display: block;
        font-size: 0.75rem;
        margin-top: 0.35rem;
    }
    .size-stats {
        border-top: 1px solid var(--border);
        display: flex;
        flex-direction: column;
        gap: 0.15rem;
        margin-top: 0.5rem;
        padding-top: 0.5rem;
    }
    .state-size {
        font-size: 1.1rem;
        font-weight: 600;
    }
    .stat-caption {
        color: var(--fg-subtle);
        font-size: 0.6875rem;
    }
    .loss-row {
        display: flex;
        font-size: 0.75rem;
        gap: 0.4rem;
        justify-content: space-between;
    }
    .loss-row span:first-child {
        color: var(--fg-subtle);
    }
    .loss-row span:last-child {
        font-variant-numeric: tabular-nums;
    }
    .rebuild-saving {
        color: color-mix(in srgb, var(--fg) 65%, #32a86a);
        font-size: 0.75rem;
        font-weight: 600;
        margin-top: 0.2rem;
    }
    .rebuild-baseline {
        color: var(--fg-subtle);
        font-size: 0.75rem;
        margin-top: 0.2rem;
    }
    .legend {
        margin-top: 0.65rem;
    }
    .estimates-help {
        margin-top: 0.5rem;
    }
    .estimates-help summary {
        color: var(--fg-link);
        cursor: pointer;
    }
    .sharing {
        border-top: 1px solid var(--border-subtle);
        margin-top: 1rem;
        padding-top: 0.75rem;
    }
    .model-row {
        align-items: center;
        display: flex;
        gap: 0.75rem;
        min-height: 2rem;
    }
    .single-model-label {
        flex: 1;
        font-weight: 600;
        margin: 0;
    }
    .profile-scope {
        border-top: 1px solid var(--border-subtle);
        margin-top: 1rem;
        padding-top: 0.75rem;
    }
    .kept-choice .per-card {
        line-height: 1.45;
    }
    @media (max-width: 600px) {
        .model-sizes {
            grid-template-columns: 1fr;
        }
        .model-size:not(.kept-choice) {
            display: grid;
            gap: 0 0.75rem;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        }
        .model-size:not(.kept-choice) .size-header {
            align-self: start;
        }
        .model-size:not(.kept-choice) .size-details {
            display: contents;
        }
        .model-size:not(.kept-choice) .per-card {
            grid-column: 1;
        }
        .model-size:not(.kept-choice) .size-stats {
            border-left: 1px solid var(--border);
            border-top: 0;
            grid-column: 2;
            grid-row: 1 / span 2;
            margin: 0;
            padding: 0 0 0 0.75rem;
        }
    }
</style>
