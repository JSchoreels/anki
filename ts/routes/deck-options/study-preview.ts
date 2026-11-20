// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    type DeckConfig_Config,
    DeckConfig_Config_NewCardGatherPriority as Gather,
    DeckConfig_Config_NewCardSortOrder as NewOrder,
    DeckConfig_Config_ReviewCardOrder as ReviewOrder,
    DeckConfig_Config_ReviewMix as ReviewMix,
    type StudySessionPreview_Card as Card,
    StudySessionPreview_DailyLimit as DailyLimit,
    StudySessionPreview_Kind as Kind,
    StudySessionPreview_Reason as Reason,
} from "@generated/anki/deck_config_pb";
import * as tr from "@generated/ftl";

import {
    newGatherPriorityChoices,
    newSortOrderChoices,
    reviewMixChoices,
    reviewOrderChoices,
    suspendedCardOrderChoices,
} from "./choices";

export function previewSample(cards: Card[], width: number): Card[] {
    const capacity = Math.max(3, Math.min(12, Math.floor((width - 25) / 105)));
    if (cards.length <= capacity) { return cards; }
    const last = (items: Card[]) => items[items.length - 1];
    const scheduled = cards.filter((c) => c.position);
    const priority = [
        scheduled[0],
        cards.find((c) => !c.position),
        scheduled.find((c) => c.kind === Kind.NEW || c.kind === Kind.SUSPENDED),
        last(scheduled.filter((c) => c.kind === Kind.REVIEW)),
        last(scheduled),
        last(scheduled.filter((c) => c.kind === Kind.NEW || c.kind === Kind.SUSPENDED)),
        ...scheduled.slice(1, 5),
        scheduled.find((c) => c.kind === Kind.SAME_DAY),
        scheduled.find((c) => c.kind === Kind.CARRIED_OVER),
        ...scheduled,
    ];
    const chosen = new Set<Card>();
    for (const card of priority) {
        if (card) { chosen.add(card); }
        if (chosen.size === capacity) { break; }
    }
    return cards.filter((c) => chosen.has(c));
}

export function kindLabel(kind: Kind): string {
    return [
        tr.deckConfigStudyNew,
        tr.deckConfigStudySuspended,
        tr.deckConfigStudyReview,
        tr.deckConfigStudySameDay,
        tr.deckConfigStudyCarried,
    ][kind]();
}

export function cardStatus(card: Card): string {
    if (card.position) { return kindLabel(card.kind); }
    if (card.reason === Reason.LATER) { return tr.deckConfigStudyLater(); }
    return card.reason === Reason.SUSPENDED ? tr.deckConfigStudyExcluded() : tr.deckConfigStudyHeld();
}

function choice(list: { value: number; label: string }[], value: number) {
    return list.find((c) => c.value === value)?.label ?? "";
}

export function usesSameDayOrder(config: DeckConfig_Config, fsrs: boolean): boolean {
    return fsrs && !config.rwkvReviewEnabled && !config.rwkvReviewInstantOrderEnabled
        && [
            ReviewOrder.RETRIEVABILITY_ASCENDING,
            ReviewOrder.RETRIEVABILITY_DESCENDING,
            ReviewOrder.RELATIVE_OVERDUENESS,
        ].includes(config.reviewOrder);
}

export function selectionSummary(config: DeckConfig_Config, newCardsIgnoreReviewLimit: boolean): string {
    const order = config.newCardGatherPriority;
    const selected = choice(newGatherPriorityChoices(), order);
    const parts = [tr.deckConfigStudyWhySelection({ order: selected })];
    if (config.includeSuspendedCardsInLearnablePool) {
        parts.push(choice(suspendedCardOrderChoices(), config.suspendedCardOrder) + ".");
    }
    if (hasSeparateDisplayOrder(order)) {
        parts.push(
            tr.deckConfigStudyWhyNewOrder({ order: choice(newSortOrderChoices(true), config.newCardSortOrder) }),
        );
    }
    parts.push(
        newCardsIgnoreReviewLimit && config.newMix === ReviewMix.AFTER_REVIEWS
            ? tr.deckConfigStudyWhyAfterReviews()
            : tr.deckConfigStudyWhyMix({ order: choice(reviewMixChoices(), config.newMix) }),
    );
    return parts.join(" ");
}

function hasSeparateDisplayOrder(order: Gather): boolean {
    return ![
        Gather.NOTE_FIELD_ASCENDING,
        Gather.NOTE_FIELD_DESCENDING,
        Gather.ASCENDING_RETRIEVABILITY,
        Gather.DESCENDING_RETRIEVABILITY,
    ].includes(order);
}

export function explainCard(
    card: Card,
    config: DeckConfig_Config,
    fsrs: boolean,
    sameDayMix: number,
    hasScores: boolean,
): string {
    if (card.reason === Reason.BURIED) { return tr.deckConfigStudyWhyBuried(); }
    const mix = (value: number) => tr.deckConfigStudyWhyMix({ order: choice(reviewMixChoices(), value) });
    const due = () => {
        if (card.due < 0) { return tr.deckConfigStudyOverdue({ days: -card.due }); }
        return card.due > 0 ? tr.deckConfigStudyDueIn({ days: card.due }) : tr.deckConfigStudyDueToday();
    };
    const details: string[] = [];
    const usesNewSlot = card.kind === Kind.NEW || card.kind === Kind.SUSPENDED;
    if (usesNewSlot) {
        const order = config.newCardGatherPriority;
        if (card.gatherPosition) { details.push(tr.deckConfigStudyWhyGather({ rank: String(card.gatherPosition) })); }
        if (order === Gather.NOTE_FIELD_ASCENDING || order === Gather.NOTE_FIELD_DESCENDING) {
            details.push(
                tr.deckConfigStudyWhyField({
                    field: config.newCardGatherField || tr.deckConfigNoteTypeSortField(),
                    value: card.fieldValue || tr.deckConfigStudyMissingField(),
                }),
            );
        } else if (card.due > 0 && [Gather.DECK, Gather.LOWEST_POSITION, Gather.HIGHEST_POSITION].includes(order)) {
            details.push(tr.deckConfigStudyDeckPosition({ position: String(card.due) }));
        }
        if (card.position) {
            if (
                card.templateNumber > 1 && hasSeparateDisplayOrder(order)
                && [NewOrder.TEMPLATE, NewOrder.TEMPLATE_THEN_RANDOM, NewOrder.RANDOM_NOTE_THEN_TEMPLATE]
                    .includes(config.newCardSortOrder)
            ) {
                details.push(tr.deckConfigStudyCardType({ number: String(card.templateNumber) }));
            }
            if (card.kind === Kind.SUSPENDED) { details.push(tr.deckConfigStudyWhySuspended()); }
        }
    } else if (card.kind === Kind.SAME_DAY) {
        details.push(
            card.due > 0
                ? tr.deckConfigStudyWhyStepLater({ minutes: String(card.due) })
                : tr.deckConfigStudyWhyStepNow(),
        );
        if (card.position && sameDayMix >= 0) { details.push(mix(sameDayMix)); }
    } else {
        details.push(due());
        if (card.position && card.kind === Kind.CARRIED_OVER) { details.push(mix(config.interdayLearningMix)); }
        if (card.position && card.kind === Kind.REVIEW) {
            if ([ReviewOrder.INTERVALS_ASCENDING, ReviewOrder.INTERVALS_DESCENDING].includes(config.reviewOrder)) {
                details.push(tr.deckConfigStudyInterval({ days: card.interval }));
            }
            const fallback = (config.rwkvReviewEnabled || config.rwkvReviewInstantOrderEnabled)
                && !(config.rwkvReviewInstantOrderEnabled && hasScores)
                && [ReviewOrder.RETRIEVABILITY_ASCENDING, ReviewOrder.RETRIEVABILITY_DESCENDING].includes(
                    config.reviewOrder,
                );
            details.push(
                tr.deckConfigStudyWhyReview({
                    order: choice(reviewOrderChoices(fsrs), fallback ? ReviewOrder.DAY : config.reviewOrder),
                }),
            );
        }
    }
    if (card.recall !== undefined) {
        details.push(
            tr.deckConfigStudyRecall({ percent: Math.round(card.recall * 100) }),
        );
    }
    const reasons: Partial<Record<Reason, () => string>> = {
        [Reason.DAILY_LIMIT]: () => {
            switch (card.dailyLimit) {
                case DailyLimit.NEW_CARDS:
                    return tr.deckConfigStudyWhyNewLimit();
                case DailyLimit.REVIEWS:
                    return usesNewSlot ? tr.deckConfigStudyWhySharedLimit() : tr.deckConfigStudyWhyReviewLimit();
                default:
                    return tr.deckConfigStudyWhyLimit();
            }
        },
        [Reason.SIBLING]: tr.deckConfigStudyWhySibling,
        [Reason.SUSPENDED]: tr.deckConfigStudyWhyExcluded,
        [Reason.MINIMUM]: tr.deckConfigStudyWhyMinimum,
        [Reason.NOT_SELECTED]: tr.deckConfigStudyWhyNotSelected,
    };
    const reason = reasons[card.reason];
    if (reason) { details.push(reason()); }
    return details.join(" ");
}
