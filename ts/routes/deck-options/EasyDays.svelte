<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import Item from "$lib/components/Item.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { DeckOptionsState } from "./lib";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import Warning from "./Warning.svelte";
    import EasyDaysInput from "./EasyDaysInput.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    const fsrsEnabled = state.fsrs;
    const reschedule = state.fsrsReschedule;
    const loadBalancerEnabled = state.loadBalancerEnabled;
    const reviewFuzzEnabled = state.reviewFuzzEnabled;
    const reviewFuzzBase = state.reviewFuzzBase;
    const reviewFuzzFactorShort = state.reviewFuzzFactorShort;
    const reviewFuzzFactorMid = state.reviewFuzzFactorMid;
    const reviewFuzzFactorLong = state.reviewFuzzFactorLong;
    const config = state.currentConfig;
    const defaults = state.defaults;
    const prevEasyDaysPercentages = $config.easyDaysPercentages.slice();
    const defaultReviewFuzzEnabled = true;
    const defaultReviewFuzzBase = 1.0;
    const defaultReviewFuzzFactorShort = 0.15;
    const defaultReviewFuzzFactorMid = 0.1;
    const defaultReviewFuzzFactorLong = 0.05;
    const prevReviewFuzzEnabled = $reviewFuzzEnabled;
    const prevReviewFuzzBase = $reviewFuzzBase;
    const prevReviewFuzzFactorShort = $reviewFuzzFactorShort;
    const prevReviewFuzzFactorMid = $reviewFuzzFactorMid;
    const prevReviewFuzzFactorLong = $reviewFuzzFactorLong;
    const previewIntervals = [3, 7, 20, 30, 90, 180];
    const settings = {
        loadBalancerEnabled: {
            title: tr.deckConfigLoadBalancerEnabled(),
            help: tr.deckConfigLoadBalancerEnabledTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
        reviewFuzzEnabled: {
            title: tr.deckConfigReviewFuzzEnabled(),
            help: tr.deckConfigReviewFuzzEnabledTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            sched: HelpItemScheduler.FSRS,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

    $: if ($config.easyDaysPercentages.length !== 7) {
        $config.easyDaysPercentages = defaults.easyDaysPercentages.slice();
    }

    $: easyDaysChanged = $config.easyDaysPercentages.some(
        (value, index) => value !== prevEasyDaysPercentages[index],
    );
    $: reviewFuzzChanged =
        $reviewFuzzEnabled !== prevReviewFuzzEnabled ||
        $reviewFuzzBase !== prevReviewFuzzBase ||
        $reviewFuzzFactorShort !== prevReviewFuzzFactorShort ||
        $reviewFuzzFactorMid !== prevReviewFuzzFactorMid ||
        $reviewFuzzFactorLong !== prevReviewFuzzFactorLong;

    $: noNormalDay = $config.easyDaysPercentages.some((p) => p === 1.0)
        ? ""
        : tr.deckConfigEasyDaysNoNormalDays();

    $: rescheduleWarning =
        (easyDaysChanged || reviewFuzzChanged) && !($fsrsEnabled && $reschedule)
            ? tr.deckConfigEasyDaysChange()
            : "";
    $: fuzzPreviewRows = previewIntervals.map((interval) => ({
        interval,
        defaultBounds: formatFuzzBounds(
            interval,
            defaultReviewFuzzEnabled,
            defaultReviewFuzzBase,
            defaultReviewFuzzFactorShort,
            defaultReviewFuzzFactorMid,
            defaultReviewFuzzFactorLong,
        ),
        currentBounds: formatFuzzBounds(
            interval,
            prevReviewFuzzEnabled,
            prevReviewFuzzBase,
            prevReviewFuzzFactorShort,
            prevReviewFuzzFactorMid,
            prevReviewFuzzFactorLong,
        ),
        selectedBounds: formatFuzzBounds(
            interval,
            $reviewFuzzEnabled,
            $reviewFuzzBase,
            $reviewFuzzFactorShort,
            $reviewFuzzFactorMid,
            $reviewFuzzFactorLong,
        ),
    }));

    function formatFuzzBounds(
        interval: number,
        enabled: boolean | undefined,
        base: number | undefined,
        factorShort: number | undefined,
        factorMid: number | undefined,
        factorLong: number | undefined,
    ): string {
        const [lower, upper] = constrainedFuzzBounds(
            interval,
            enabled,
            base,
            factorShort,
            factorMid,
            factorLong,
        );
        return lower === upper ? `${lower}d` : `${lower}-${upper}d`;
    }

    function constrainedFuzzBounds(
        interval: number,
        enabled: boolean | undefined,
        base: number | undefined,
        factorShort: number | undefined,
        factorMid: number | undefined,
        factorLong: number | undefined,
        minimum = 1,
        maximum = 36500,
    ): [number, number] {
        const clampedInterval = Math.min(Math.max(interval, minimum), maximum);
        const delta = fuzzDelta(
            clampedInterval,
            enabled,
            base,
            factorShort,
            factorMid,
            factorLong,
        );
        let lower = Math.round(clampedInterval - delta);
        let upper = Math.round(clampedInterval + delta);
        lower = Math.min(Math.max(lower, minimum), maximum);
        upper = Math.min(Math.max(upper, minimum), maximum);
        if (upper === lower && upper > 2 && upper < maximum) {
            upper = lower + 1;
        }
        return [lower, upper];
    }

    function fuzzDelta(
        interval: number,
        enabled: boolean | undefined,
        base: number | undefined,
        factorShort: number | undefined,
        factorMid: number | undefined,
        factorLong: number | undefined,
    ): number {
        if (!enabled || interval < 2.5) {
            return 0;
        }
        return (
            (base ?? 1.0) +
            (factorShort ?? 0.15) * Math.max(0, Math.min(interval, 7) - 2.5) +
            (factorMid ?? 0.1) * Math.max(0, Math.min(interval, 20) - 7) +
            (factorLong ?? 0.05) * Math.max(0, interval - 20)
        );
    }

    let modal: Modal;
    let carousel: Carousel;

    function openHelpModal(key: keyof typeof settings): void {
        modal.show();
        carousel.to(Object.keys(settings).indexOf(key));
    }
</script>

<datalist id="easy_day_steplist">
    <option>0.5</option>
</datalist>

<TitledContainer title={tr.deckConfigEasyDaysTitle()}>
    <HelpModal
        title={tr.deckConfigEasyDaysTitle()}
        url={HelpPage.DeckOptions.fsrs}
        slot="tooltip"
        fsrs={$fsrsEnabled}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <Item>
            <SwitchRow bind:value={$loadBalancerEnabled} defaultValue={false}>
                <SettingTitle on:click={() => openHelpModal("loadBalancerEnabled")}>
                    <GlobalLabel title={tr.deckConfigLoadBalancerEnabled()} />
                </SettingTitle>
            </SwitchRow>
        </Item>
        <Item>
            <SwitchRow
                bind:value={$reviewFuzzEnabled}
                defaultValue={defaultReviewFuzzEnabled}
            >
                <SettingTitle on:click={() => openHelpModal("reviewFuzzEnabled")}>
                    <GlobalLabel title={tr.deckConfigReviewFuzzEnabled()} />
                </SettingTitle>
            </SwitchRow>
        </Item>
        <EasyDaysInput bind:values={$config.easyDaysPercentages} />
        <Item>
            <div class="review-fuzz-title">{tr.deckConfigReviewFuzzTitle()}</div>
        </Item>
        <Item>
            <SpinBoxFloatRow
                bind:value={$reviewFuzzBase}
                defaultValue={defaultReviewFuzzBase}
                min={0}
                max={10}
                step={0.1}
            >
                {tr.deckConfigReviewFuzzBase()}
            </SpinBoxFloatRow>
        </Item>
        <Item>
            <SpinBoxFloatRow
                bind:value={$reviewFuzzFactorShort}
                defaultValue={defaultReviewFuzzFactorShort}
                min={0}
                max={1}
                step={0.01}
                percentage={true}
            >
                {tr.deckConfigReviewFuzzFactorShort()}
            </SpinBoxFloatRow>
        </Item>
        <Item>
            <SpinBoxFloatRow
                bind:value={$reviewFuzzFactorMid}
                defaultValue={defaultReviewFuzzFactorMid}
                min={0}
                max={1}
                step={0.01}
                percentage={true}
            >
                {tr.deckConfigReviewFuzzFactorMid()}
            </SpinBoxFloatRow>
        </Item>
        <Item>
            <SpinBoxFloatRow
                bind:value={$reviewFuzzFactorLong}
                defaultValue={defaultReviewFuzzFactorLong}
                min={0}
                max={1}
                step={0.01}
                percentage={true}
            >
                {tr.deckConfigReviewFuzzFactorLong()}
            </SpinBoxFloatRow>
        </Item>
        <Item>
            <div class="review-fuzz-preview">
                <div class="review-fuzz-preview-title">
                    {tr.deckConfigReviewFuzzPreview()}
                </div>
                <table class="review-fuzz-preview-table">
                    <thead>
                        <tr>
                            <th></th>
                            <th>{tr.deckConfigDefaultFuzz()}</th>
                            <th>{tr.deckConfigCurrentFuzz()}</th>
                            <th>{tr.deckConfigSelectedFuzz()}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each fuzzPreviewRows as row}
                            <tr>
                                <th>{row.interval}d</th>
                                <td>{row.defaultBounds}</td>
                                <td>{row.currentBounds}</td>
                                <td>{row.selectedBounds}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        </Item>
        <Item>
            <Warning warning={noNormalDay} />
        </Item>
        <Item>
            <Warning warning={rescheduleWarning} />
        </Item>
    </DynamicallySlottable>
</TitledContainer>

<style>
    .review-fuzz-title {
        font-weight: 600;
    }

    .review-fuzz-preview {
        margin: 0 0.25rem;
    }

    .review-fuzz-preview-title {
        font-weight: 600;
        margin-bottom: 0.35rem;
    }

    .review-fuzz-preview-table {
        width: 100%;
        font-size: 0.9rem;
        border-collapse: collapse;
    }

    .review-fuzz-preview-table th,
    .review-fuzz-preview-table td {
        padding: 0.25rem 0.5rem;
        text-align: left;
        border-bottom: 1px solid var(--border);
    }

    .review-fuzz-preview-table thead th {
        font-weight: 600;
    }
</style>
