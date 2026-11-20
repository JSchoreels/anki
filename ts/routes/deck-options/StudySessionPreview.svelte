<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        PreviewStudySessionRequest,
        PreviewStudySessionRequest_Source as Source,
        type StudySessionPreview,
        type StudySessionPreview_Card as Card,
        UpdateDeckConfigsMode,
    } from "@generated/anki/deck_config_pb";
    import { previewStudySession } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { onMount } from "svelte";
    import type { Readable } from "svelte/store";
    import Choice from "$lib/components/EnumSelector.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import { readDisplayOrderSettings } from "./display-order-settings";
    import type { DeckOptionsState } from "./lib";
    import {
        cardStatus,
        explainCard,
        previewSample,
        selectionSummary,
        usesSameDayOrder,
    } from "./study-preview";

    export let state: DeckOptionsState;
    const config = state.currentConfig;
    const fsrs = state.fsrs;
    const aux = state.currentAuxData;
    const sources = [
        { value: Source.DECK, label: state.currentDeck.name },
        { value: Source.NUMBERED, label: tr.deckConfigStudyNumbered() },
        { value: Source.STEPS, label: tr.deckConfigStudyStepsExample() },
    ];
    let source = Source.DECK;
    let preview: StudySessionPreview | undefined;
    let error = "";
    let highlighted: bigint | undefined;
    let panel: HTMLElement;
    let width = 600;
    let generation = 0;
    let timer: ReturnType<typeof setTimeout>;
    let mounted = false;
    let pending = false;
    let working = false;
    let previous: PreviewStudySessionRequest | undefined;
    let displayedSameDayMix = -1;

    function schedule() {
        ++generation;
        pending = true;
        clearTimeout(timer);
        timer = setTimeout(refresh, 180);
    }
    async function refresh() {
        if (working || !mounted) {
            return;
        }
        working = true;
        pending = false;
        const version = generation;
        try {
            // Nested protobuf messages can be the same mutable objects as the form.
            const request = new PreviewStudySessionRequest({
                source,
                options: state.dataForSaving(UpdateDeckConfigsMode.NORMAL),
            }).clone();
            if (!previous?.equals(request)) {
                const result = await previewStudySession(request, {
                    alertOnError: false,
                });
                if (mounted && version === generation) {
                    preview = result;
                    previous = request;
                    displayedSameDayMix = usesSameDayOrder($config, $fsrs)
                        ? readDisplayOrderSettings($aux).intradayLearningMix
                        : -1;
                    highlighted = undefined;
                    error = "";
                }
            }
        } catch (err) {
            if (version === generation && mounted) {
                previous = undefined;
                preview = undefined;
                error = String(err);
            }
        } finally {
            working = false;
            if (mounted && pending) {
                clearTimeout(timer);
                timer = setTimeout(refresh, 0);
            }
        }
    }
    onMount(() => {
        mounted = true;
        const settings: Readable<unknown>[] = [
            config,
            aux,
            fsrs,
            state.deckLimits,
            state.configList,
            state.applyAllParentLimits,
            state.newCardsIgnoreReviewLimit,
            state.fsrsShortTermWithStepsEnabled,
            state.rwkvRespectSubdeckMinimums,
            state.rwkvApplyRespectSubdeckMinimumsToChildren,
            state.fsrsLearningQueuesDisabled,
        ];
        const subscriptions = settings.map((s) => s.subscribe(schedule));
        const observer = new ResizeObserver(([entry]) => {
            width = entry.contentRect.width;
        });
        observer.observe(panel);
        return () => {
            mounted = false;
            clearTimeout(timer);
            observer.disconnect();
            subscriptions.forEach((s) => s());
        };
    });
    $: shown = previewSample(preview?.cards ?? [], width);
    $: displayedConfig = previous?.options?.configs.at(-1)?.config ?? $config;
    $: displayedFsrs = previous?.options?.fsrs ?? $fsrs;
    function gapBefore(cards: Card[], index: number, total: number) {
        return index
            ? (cards[index].position || total + 1) - cards[index - 1].position - 1
            : 0;
    }
</script>

<TitledContainer title={tr.deckConfigStudyPreview()}>
    <span slot="header-actions">
        <Choice
            inlineValue
            accessibleLabel={tr.deckConfigStudyPreviewSource()}
            choices={sources}
            bind:value={source}
            on:change={schedule}
        />
    </span>
    <section bind:this={panel} aria-label={tr.deckConfigStudyPreview()}>
        {#if error}
            <p role="status" title={error}>{tr.deckConfigStudyPreviewFailed()}</p>
        {:else if !preview}
            <p role="status">{tr.deckConfigStudyPreviewLoading()}</p>
        {:else}
            <p class="caption" aria-live="polite">
                {tr.deckConfigStudyPreviewCount({ count: preview.total })}
                {#if preview.newCount || preview.reviewCount || preview.learningCount}
                    · {tr.deckConfigStudyQueuedReviews({ count: preview.reviewCount })}
                    · {tr.deckConfigStudyQueuedNew({ count: preview.newCount })}
                    {#if preview.learningCount}
                        · {tr.deckConfigStudyQueuedLearning({
                            count: preview.learningCount,
                        })}{/if}
                {/if}
            </p>
            {#if !preview.total}<p>{tr.deckConfigStudyPreviewEmpty()}</p>{/if}
            {#if shown.length}
                <ol class="rail">
                    {#each shown as card, index (card.id)}
                        {@const gap = gapBefore(shown, index, preview.total)}
                        {#if gap > 0}<li
                                class="gap"
                                aria-label={tr.deckConfigStudyGap({ count: gap })}
                                title={tr.deckConfigStudyGap({ count: gap })}
                            >
                                …
                            </li>{/if}
                        <li class:held={!card.position}>
                            <button
                                type="button"
                                aria-pressed={highlighted === card.id}
                                on:click={() => (highlighted = card.id)}
                            >
                                <small>
                                    {card.position || "—"} · {cardStatus(card)}
                                </small>
                                <strong>{card.label}</strong>
                            </button>
                        </li>
                    {/each}
                </ol>
                <h2>{tr.deckConfigStudyWhyTitle()}</h2>
                {#if preview.cards.some((c) => c.gatherPosition)}<p class="caption">
                        {selectionSummary(
                            displayedConfig,
                            previous?.options?.newCardsIgnoreReviewLimit ?? false,
                        )}
                    </p>{/if}
                <ul
                    class="reasons"
                    aria-label={tr.deckConfigStudyWhyTitle()}
                    style:--position-width={`${Math.max(2, String(preview.total).length)}ch`}
                >
                    {#each shown as card, index (card.id)}
                        {@const gap = gapBefore(shown, index, preview.total)}
                        {#if gap > 0}<li class="omitted">
                                <span>…</span>
                                <span>{tr.deckConfigStudyGap({ count: gap })}</span>
                            </li>{/if}
                        <li
                            class:highlight={highlighted === card.id}
                            aria-current={highlighted === card.id ? "true" : undefined}
                            class:held={!card.position}
                        >
                            <span>{card.position || "—"}</span>
                            <strong>{card.label}</strong>
                            <span>
                                {explainCard(
                                    card,
                                    displayedConfig,
                                    displayedFsrs,
                                    displayedSameDayMix,
                                    preview.hasRwkvScores,
                                )}
                            </span>
                        </li>
                    {/each}
                </ul>
            {/if}
            <p class="caption">
                {tr.deckConfigStudyPreviewUnsaved()}
                {#if displayedConfig.rwkvReviewInstantOrderEnabled && !preview.hasRwkvScores}{tr.deckConfigStudyPreviewScoreFallback()}{/if}
                {#if previous?.source !== Source.DECK && preview.hasRwkvScores}{tr.deckConfigStudyExampleScores()}{/if}
            </p>
        {/if}
    </section>
</TitledContainer>

<style>
    section {
        min-width: 0;
    }
    .caption,
    small,
    .reasons {
        color: var(--fg-subtle);
        font-size: 0.82rem;
    }
    p,
    ol,
    ul {
        margin: 0.45rem 0;
    }
    .rail,
    .reasons {
        list-style: none;
        padding: 0;
    }
    .rail {
        display: flex;
        align-items: stretch;
        gap: 0.35rem;
        flex-wrap: wrap;
        margin-bottom: 0.75rem;
    }
    .rail li {
        flex: 1 1 4.5rem;
        min-width: 0;
    }
    .rail button {
        text-align: start;
        display: grid;
        width: 100%;
        height: 100%;
        padding: 0.4rem 0.55rem;
        gap: 0.2rem;
        background: var(--canvas-inset);
        color: var(--fg);
        box-shadow: none;
        border: 1px solid var(--border-subtle);
        border-radius: 0.45rem;
    }
    .rail strong {
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        font-weight: 550;
    }
    .rail small {
        font-size: 0.7rem;
    }
    .rail .gap {
        flex: 0 0 0.85rem;
        align-self: center;
        color: var(--fg-subtle);
    }
    .rail .held button {
        background: transparent;
        color: var(--fg-subtle);
        border-style: dashed;
    }
    .reasons li {
        display: grid;
        grid-template-columns: var(--position-width) minmax(4rem, 16%) 1fr;
        gap: 0.55rem;
        align-items: baseline;
        padding: 0.22rem 0.35rem;
        border-radius: 0.25rem;
    }
    .reasons li > :first-child {
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
    }
    .reasons strong {
        font-weight: 550;
        color: var(--fg);
        overflow-wrap: anywhere;
    }
    .reasons .held strong {
        color: var(--fg-subtle);
    }
    .reasons .highlight {
        background: var(--canvas-inset);
        box-shadow: inset 2px 0 var(--border-focus);
    }
    .reasons .omitted {
        grid-template-columns: var(--position-width) 1fr;
    }
    h2 {
        font-size: 0.95rem;
        margin: 0.6rem 0 0.25rem;
    }
    @media (max-width: 420px) {
        .reasons li {
            grid-template-columns: var(--position-width) minmax(3rem, 22%) 1fr;
            gap: 0.3rem;
        }
    }
</style>
