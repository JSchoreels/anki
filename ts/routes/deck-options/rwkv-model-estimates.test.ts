// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { rwkvModelEstimates, rwkvModelLayout } from "./rwkv-model-estimates";

describe("RWKV model estimates", () => {
    test("uses card and note counts separately for paired and shared models", () => {
        const counts = { cards: 1_000, notes: 500 };
        const usage = { curve: true, instant: true };
        const paired = rwkvModelEstimates(usage, false, "compact_cards_int8", counts);
        expect(paired.map(({ perCardAndNote }) => perCardAndNote)).toEqual([46_354, 7_758, 5_298]);
        expect(paired.map(({ totalStateBytes }) => totalStateBytes)).toEqual([37_077_500, 6_433_500, 3_973_500]);
        // Replay savings are measured at the default (the low-rank state) only.
        expect(paired.map(({ replaySaving }) => replaySaving)).toEqual([undefined, undefined, undefined]);
        expect(
            rwkvModelEstimates(usage, false, "compact_lowrank_int8", counts).map(({ replaySaving }) => replaySaving),
        ).toEqual([0, 72, 84]);
        const shared = rwkvModelEstimates(usage, true, "compact_cards_int8", counts);
        expect(shared.map(({ totalStateBytes }) => totalStateBytes)).toEqual([18_524_500, 3_202_500, 1_972_500]);
        expect(shared.map(({ curveLoss }) => curveLoss)).toEqual(paired.map(({ curveLoss }) => curveLoss));
        expect(shared.map(({ instantLoss }) => instantLoss)).toEqual([0, 0.15, 0.68]);
    });

    test("uses the selected precision and omits totals when counts are unavailable", () => {
        const usage = { curve: true, instant: false };
        expect(rwkvModelEstimates(usage, false, "f32", undefined).map(({ perCardAndNote }) => perCardAndNote))
            .toEqual([87_136, 13_640, 9_276]);
        expect(
            rwkvModelEstimates(usage, false, "compact_cards_int4", undefined).map(({ perCardAndNote }) =>
                perCardAndNote
            ),
        ).toEqual([17_014, 2_836, 2_118]);
        // The former default stays readable for estimates of an old profile.
        expect(
            rwkvModelEstimates(usage, false, "cards_int4_notes_int8", undefined).map(({ replaySaving }) =>
                replaySaving
            ),
        ).toEqual([undefined, undefined, undefined]);
        // Unknown values use the default (the low-rank state: mean bytes over
        // the answer counts of the benchmark users).
        expect(rwkvModelEstimates(usage, false, "unknown", undefined).map(({ perCardAndNote }) => perCardAndNote))
            .toEqual([4_244, 1_309, 932]);
        const both = { curve: true, instant: true };
        const lowRank = rwkvModelEstimates(both, false, "compact_lowrank_int8", undefined);
        expect(lowRank.map(({ perCardAndNote }) => perCardAndNote)).toEqual([8_526, 2_656, 1_902]);
        // The tooltip range: rank 1 (one answer) to rank 4 (16 answers or more).
        expect(lowRank.map(({ perCardAndNoteRange }) => perCardAndNoteRange)).toEqual([
            [5_574, 13_734],
            [1_722, 4_314],
            [1_274, 3_002],
        ]);
        expect(
            rwkvModelEstimates(usage, false, "compact_lowrank_int8", undefined).map(({ perCardAndNoteRange }) =>
                perCardAndNoteRange
            ),
        ).toEqual([[2_768, 6_848], [842, 2_138], [618, 1_482]]);
        expect(
            rwkvModelEstimates(usage, false, "compact_cards_int8", undefined).map(({ perCardAndNoteRange }) =>
                perCardAndNoteRange
            ),
        ).toEqual([undefined, undefined, undefined]);
        const int8 = rwkvModelEstimates(usage, false, "int8", undefined);
        expect(int8.map(({ perCardAndNote }) => perCardAndNote)).toEqual([25_698, 4_554, 3_134]);
        expect(int8.map(({ totalStateBytes }) => totalStateBytes)).toEqual([undefined, undefined, undefined]);
        expect(int8.map(({ instantLoss }) => instantLoss)).toEqual([undefined, undefined, undefined]);
        expect(int8.map(({ replaySaving }) => replaySaving)).toEqual([undefined, undefined, undefined]);
    });

    test("ignores sharing with only one scheduler and hides unavailable Curve loss", () => {
        const instantOnly = { curve: false, instant: true };
        expect(rwkvModelLayout(instantOnly, true)).toBe("instant");
        expect(rwkvModelEstimates(instantOnly, true, "int8", { cards: 0, notes: 0 }))
            .toEqual(rwkvModelEstimates(instantOnly, false, "int8", { cards: 0, notes: 0 }));
        const estimates = rwkvModelEstimates(instantOnly, true, "int8", { cards: 0, notes: 0 });
        expect(estimates.map(({ curveLoss }) => curveLoss)).toEqual([undefined, undefined, undefined]);
        expect(estimates.map(({ totalStateBytes }) => totalStateBytes)).toEqual([0, 0, 0]);
    });
});
