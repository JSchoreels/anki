<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { createEventDispatcher } from "svelte";
    import * as tr from "@generated/ftl";
    import { setProfileConfig } from "@tslib/profile";
    import type { Writable } from "svelte/store";

    import "$lib/sveltelib/export-runtime";

    import Container from "$lib/components/Container.svelte";
    import Row from "$lib/components/Row.svelte";
    import type { DynamicSvelteComponent } from "$lib/sveltelib/dynamicComponent";

    import Addons from "./Addons.svelte";
    import AdvancedOptions from "./AdvancedOptions.svelte";
    import AudioOptions from "./AudioOptions.svelte";
    import AutoAdvance from "./AutoAdvance.svelte";
    import BuryOptions from "./BuryOptions.svelte";
    import ConfigSelector from "./ConfigSelector.svelte";
    import DailyLimits from "./DailyLimits.svelte";
    import DisplayOrder from "./DisplayOrder.svelte";
    import FsrsOptionsOuter from "./FsrsOptionsOuter.svelte";
    import HtmlAddon from "./HtmlAddon.svelte";
    import LapseOptions from "./LapseOptions.svelte";
    import type { DeckOptionsState } from "./lib";
    import NewOptions from "./NewOptions.svelte";
    import RwkvOptions from "./RwkvOptions.svelte";
    import TimerOptions from "./TimerOptions.svelte";
    import EasyDays from "./EasyDays.svelte";
    import StudySessionPreview from "./StudySessionPreview.svelte";
    import "./study-options.scss";

    export let state: DeckOptionsState;
    export let classic = false;
    $: study = !classic;

    // Keep the same mounted controls and add-on slot hosts in both layouts.
    let behavior: HTMLElement;
    let reviewBehavior: HTMLElement;
    let classicBehavior: HTMLElement;
    $: if (behavior && reviewBehavior && classicBehavior) {
        (classic ? classicBehavior : reviewBehavior).appendChild(behavior);
    }
    let advancedBlock: HTMLElement;
    let easyDaysBlock: HTMLElement;
    $: if (advancedBlock && easyDaysBlock) {
        if (study) {
            advancedBlock.after(easyDaysBlock);
        } else {
            advancedBlock.before(easyDaysBlock);
        }
    }
    const dispatch = createEventDispatcher<{ close: void }>();
    const addons = state.addonComponents;

    export function auxData(): Writable<Record<string, unknown>> {
        return state.currentAuxData;
    }

    export function addSvelteAddon(component: DynamicSvelteComponent): void {
        $addons = [...$addons, component];
    }

    export function addHtmlAddon(html: string, mounted: () => void): void {
        $addons = [
            ...$addons,
            {
                component: HtmlAddon,
                html,
                mounted,
            },
        ];
    }

    export const options = {};
    export const dailyLimits = {};
    export const newOptions = {};
    export const lapseOptions = {};
    export const buryOptions = {};
    export const displayOrder = {};
    export const timerOptions = {};
    export const audioOptions = {};
    export const advancedOptions = {};
    export const easyDays = {};

    let dailyLimitsComponent: DailyLimits | undefined;
    let fsrsOptionsOuterComponent: FsrsOptionsOuter | undefined;

    function onPresetChange() {
        if (dailyLimitsComponent) {
            dailyLimitsComponent.onPresetChange();
        }
        if (fsrsOptionsOuterComponent) {
            fsrsOptionsOuterComponent.onPresetChange();
        }
    }
</script>

<ConfigSelector
    {state}
    on:presetchange={onPresetChange}
    on:close={() => dispatch("close")}
>
    <label
        class="layout-switch form-check form-switch"
        title={tr.deckConfigStudyClassicHelp()}
    >
        <input
            class="form-check-input"
            type="checkbox"
            role="switch"
            bind:checked={classic}
            on:change={(e) =>
                setProfileConfig("deckOptionsClassicView", e.currentTarget.checked)}
        />
        {tr.deckConfigStudyClassic()}
    </label>
</ConfigSelector>

<div class="deck-options-page" class:study>
    <Container
        breakpoint="sm"
        --gutter-inline="0.25rem"
        --gutter-block="0.75rem"
        class="container-columns"
    >
        <div>
            {#if study}<Row class="row-columns">
                    <StudySessionPreview {state} />
                </Row>{/if}
            <Row class="row-columns">
                <DailyLimits
                    {state}
                    {study}
                    api={dailyLimits}
                    bind:this={dailyLimitsComponent}
                />
            </Row>

            <Row class="row-columns">
                <NewOptions {state} {study} api={newOptions} />
            </Row>

            <Row class="row-columns">
                <LapseOptions {state} {study} api={lapseOptions}>
                    <div bind:this={reviewBehavior}></div>
                </LapseOptions>
            </Row>

            <div class:extension-host={study}>
                <DisplayOrder {state} {study} api={displayOrder} />
            </div>

            <FsrsOptionsOuter
                {state}
                {study}
                api={{}}
                bind:this={fsrsOptionsOuterComponent}
            >
                <Row class="row-columns" slot="rwkv">
                    <RwkvOptions {state} {study} {onPresetChange} />
                </Row>
            </FsrsOptionsOuter>
        </div>

        <div>
            <div bind:this={classicBehavior}>
                <div bind:this={behavior} class:study-behavior={study}>
                    {#if study}<h2>{tr.deckConfigStudyBehavior()}</h2>{/if}
                    <Row class="row-columns">
                        <BuryOptions {state} {study} api={buryOptions} />
                    </Row>

                    <Row class="row-columns">
                        <AudioOptions {state} {study} api={audioOptions} />
                    </Row>

                    <Row class="row-columns">
                        <TimerOptions {state} {study} api={timerOptions} />
                    </Row>

                    <Row class="row-columns">
                        <AutoAdvance {state} {study} api={timerOptions} />
                    </Row>
                </div>
            </div>

            {#if $addons.length}
                <Row class="row-columns">
                    <Addons {state} />
                </Row>
            {/if}

            <div bind:this={advancedBlock}>
                <Row class="row-columns">
                    <AdvancedOptions {state} {study} api={advancedOptions} />
                </Row>
            </div>
            <div bind:this={easyDaysBlock}>
                <Row class="row-columns">
                    <EasyDays {state} {study} api={easyDays} />
                </Row>
            </div>
        </div>
    </Container>
</div>

<style lang="scss">
    @use "$lib/sass/breakpoints" as bp;

    .deck-options-page {
        overflow-x: hidden;
        word-break: break-word;

        :global(.container-columns) {
            display: grid;
            gap: 0px;
        }

        @include bp.with-breakpoint("lg") {
            :global(.container-columns) {
                grid-template-columns: repeat(2, 1fr);
                gap: 20px;
            }
        }
    }
    .layout-switch {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        margin-inline-end: 1rem;
        white-space: nowrap;
        cursor: pointer;
        font-size: 0.9rem;
        padding-inline-start: 0;
        margin-block: 0;
    }
    .layout-switch .form-check-input {
        float: none;
        flex: none;
        margin: 0;
        height: 1.1rem;
        width: 2.05rem;
    }
    .deck-options-page.study :global(.container-columns) {
        display: block;
        max-width: 70rem;
        margin-inline: auto;
    }
    .study-behavior h2 {
        margin: 0.8rem 0 0.3rem;
        padding-top: 0.65rem;
        border-top: 1px solid var(--border);
        font-size: 1.05rem;
    }
    .study-behavior :global(.row-columns) {
        display: block;
        padding: 0;
    }
    .extension-host {
        display: contents;
    }
</style>
