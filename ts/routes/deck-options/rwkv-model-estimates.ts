// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { RWKV_MODEL_BIG, RWKV_MODEL_SMALL, RWKV_MODEL_TINY, type RwkvSchedulerUsage } from "./lib";

export type RwkvModelSize = typeof RWKV_MODEL_BIG | typeof RWKV_MODEL_SMALL | typeof RWKV_MODEL_TINY;
export type RwkvModelLayout = "paired" | "shared" | "curve" | "instant";

export interface RwkvModelEstimate {
    size: RwkvModelSize;
    perCardAndNote: number;
    totalStateBytes: number | undefined;
    curveLoss: number | undefined;
    instantLoss: number | undefined;
    replaySaving: number | undefined;
}

type StateSizes = { card: number; note: number };
type Precision = "f32" | "int8" | "cards_int4_notes_int8";

// Serialized card and note states, checked against the native codecs in
// rwkv_state_precision_stored_sizes_of_the_small_and_tiny_models and
// rwkv_big_pair_stores_two_big_states_per_card_and_note.
const STATE_SIZES: Record<RwkvModelSize, Record<Precision, StateSizes>> = {
    [RWKV_MODEL_BIG]: {
        f32: { card: 52_278, note: 34_858 },
        int8: { card: 15_415, note: 10_283 },
        cards_int4_notes_int8: { card: 9_271, note: 10_283 },
    },
    [RWKV_MODEL_SMALL]: {
        f32: { card: 9_002, note: 4_638 },
        int8: { card: 2_987, note: 1_567 },
        cards_int4_notes_int8: { card: 1_963, note: 1_567 },
    },
    [RWKV_MODEL_TINY]: {
        f32: { card: 4_638, note: 4_638 },
        int8: { card: 1_567, note: 1_567 },
        cards_int4_notes_int8: { card: 1_055, note: 1_567 },
    },
};

// Relative Factory LogLoss on 722 held-out users, compared to Big's matching
// Curve and Instant models. The Instant baseline also changes in shared mode.
const LOSSES: Record<RwkvModelSize, { curve: number; instant: number; sharedInstant: number }> = {
    [RWKV_MODEL_BIG]: { curve: 0, instant: 0, sharedInstant: 0 },
    [RWKV_MODEL_SMALL]: { curve: 0.31, instant: 0.11, sharedInstant: 0.25 },
    [RWKV_MODEL_TINY]: { curve: 0.42, instant: 0.30, sharedInstant: 0.43 },
};

// Median native replay time, with four threads and the default state
// precision, on 32,000 synthetic answers. See docs/rwkv.MD for the complete
// benchmark and its scope; this excludes history loading and cache writes.
const REPLAY_SAVINGS: Record<RwkvModelLayout, Record<RwkvModelSize, number>> = {
    paired: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 77, [RWKV_MODEL_TINY]: 79 },
    shared: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 78 },
    curve: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 78 },
    instant: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 79 },
};

export function rwkvModelLayout(usage: RwkvSchedulerUsage, singleModel: boolean): RwkvModelLayout {
    if (usage.curve && usage.instant) {
        return singleModel ? "shared" : "paired";
    }
    return usage.instant ? "instant" : "curve";
}

export function rwkvModelEstimates(
    usage: RwkvSchedulerUsage,
    singleModel: boolean,
    statePrecision: string,
    counts: { cards: number; notes: number } | undefined,
): RwkvModelEstimate[] {
    const layout = rwkvModelLayout(usage, singleModel);
    const precision: Precision = statePrecision === "f32" || statePrecision === "int8"
        ? statePrecision
        : "cards_int4_notes_int8";
    return ([RWKV_MODEL_BIG, RWKV_MODEL_SMALL, RWKV_MODEL_TINY] as const).map((size) => {
        let { card, note } = STATE_SIZES[size][precision];
        if (layout === "paired") {
            // Each paired stream has a 19-byte wrapper around its two states.
            card = card * 2 + 19;
            note = note * 2 + 19;
        }
        const losses = LOSSES[size];
        const instantLoss = layout === "shared" ? losses.sharedInstant : losses.instant;
        return {
            size,
            perCardAndNote: card + note,
            totalStateBytes: counts ? card * counts.cards + note * counts.notes : undefined,
            curveLoss: usage.curve ? losses.curve : undefined,
            instantLoss: usage.instant ? instantLoss : undefined,
            replaySaving: precision === "cards_int4_notes_int8" ? REPLAY_SAVINGS[layout][size] : undefined,
        };
    });
}
