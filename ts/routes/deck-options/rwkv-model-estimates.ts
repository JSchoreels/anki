// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    RWKV_DEFAULT_STATE_PRECISION,
    RWKV_MODEL_BIG,
    RWKV_MODEL_SMALL,
    RWKV_MODEL_TINY,
    type RwkvSchedulerUsage,
} from "./lib";

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
type Precision =
    | "f32"
    | "int8"
    | "cards_int4_notes_int8"
    | "compact_cards_int8"
    | "compact_cards_int4"
    | "compact_lowrank_int8";

// Serialized card and note states, checked against the native codecs in
// rwkv_state_precision_stored_sizes_of_the_small_and_tiny_models,
// rwkv_state_precision_stored_sizes_of_the_big_model and
// rwkv_big_pair_stores_two_big_states_per_card_and_note. The low-rank state
// grows with a card's answers (rank 1 after one answer, rank 2, rank 4 from
// the 16th; rwkv_state_precision_lowrank_sizes_grow_with_the_answer_count):
// its entries are the mean over the answer counts of 723 benchmark users.
const STATE_SIZES: Record<RwkvModelSize, Record<Precision, StateSizes>> = {
    [RWKV_MODEL_BIG]: {
        f32: { card: 52_278, note: 34_858 },
        int8: { card: 15_415, note: 10_283 },
        cards_int4_notes_int8: { card: 9_271, note: 10_283 },
        compact_cards_int8: { card: 13_891, note: 9_267 },
        compact_cards_int4: { card: 7_747, note: 9_267 },
        compact_lowrank_int8: { card: 2_524, note: 1_720 },
    },
    [RWKV_MODEL_SMALL]: {
        f32: { card: 9_002, note: 4_638 },
        int8: { card: 2_987, note: 1_567 },
        cards_int4_notes_int8: { card: 1_963, note: 1_567 },
        compact_cards_int8: { card: 2_545, note: 1_315 },
        compact_cards_int4: { card: 1_521, note: 1_315 },
        compact_lowrank_int8: { card: 839, note: 470 },
    },
    [RWKV_MODEL_TINY]: {
        f32: { card: 4_638, note: 4_638 },
        int8: { card: 1_567, note: 1_567 },
        cards_int4_notes_int8: { card: 1_055, note: 1_567 },
        compact_cards_int8: { card: 1_315, note: 1_315 },
        compact_cards_int4: { card: 803, note: 1_315 },
        compact_lowrank_int8: { card: 462, note: 470 },
    },
};

// Relative Factory LogLoss on 722 held-out users, compared to Big's matching
// Curve and Instant models. The Instant baseline also changes in shared mode.
// Show-time inputs, w8+s8, the release candidate's models (2026-11): Big
// curve 0.299415 and instant 0.276183 (SM91s2), Instant-Only 0.275894
// (SM99); Small curve 0.300016 and instant 0.276599 (SM118), Instant-Only
// 0.275933 (SM113); Tiny curve 0.301416 and instant 0.278049 (six-layer
// SM130 two-head), Instant-Only 0.27749 (six-layer SM130 Instant-Only).
const LOSSES: Record<RwkvModelSize, { curve: number; instant: number; sharedInstant: number }> = {
    [RWKV_MODEL_BIG]: { curve: 0, instant: 0, sharedInstant: 0 },
    [RWKV_MODEL_SMALL]: { curve: 0.20, instant: 0.01, sharedInstant: 0.15 },
    [RWKV_MODEL_TINY]: { curve: 0.67, instant: 0.58, sharedInstant: 0.68 },
};

// Median native replay time, with four threads and the default state
// precision, on 32,000 synthetic answers. See docs/rwkv.MD for the complete
// benchmark and its scope; this excludes history loading and cache writes.
// Tiny: the six-layer Tiny (SM130), rwkv_model_size_benchmark of the 2026-11
// release candidate (docs/rwkv-rc-2026-11.MD); Small has the same
// architecture as before.
const REPLAY_SAVINGS: Record<RwkvModelLayout, Record<RwkvModelSize, number>> = {
    paired: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 77, [RWKV_MODEL_TINY]: 88 },
    shared: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 87 },
    curve: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 87 },
    instant: { [RWKV_MODEL_BIG]: 0, [RWKV_MODEL_SMALL]: 76, [RWKV_MODEL_TINY]: 87 },
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
    const precision: Precision = statePrecision in STATE_SIZES[RWKV_MODEL_BIG]
        ? statePrecision as Precision
        : RWKV_DEFAULT_STATE_PRECISION;
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
            replaySaving: precision === RWKV_DEFAULT_STATE_PRECISION ? REPLAY_SAVINGS[layout][size] : undefined,
        };
    });
}
