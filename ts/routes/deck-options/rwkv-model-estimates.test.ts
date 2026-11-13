// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { rwkvModelEstimates, rwkvModelLayout } from "./rwkv-model-estimates";

describe("RWKV model estimates", () => {
    test("uses card and note counts separately for paired and shared models", () => {
        const counts = { cards: 1_000, notes: 500 };
        const usage = { curve: true, instant: true };
        const paired = rwkvModelEstimates(usage, false, "cards_int4_notes_int8", counts);
        expect(paired.map(({ perCardAndNote }) => perCardAndNote)).toEqual([39_146, 7_098, 5_282]);
        expect(paired.map(({ totalStateBytes }) => totalStateBytes)).toEqual([28_853_500, 5_521_500, 3_705_500]);
        expect(paired.map(({ replaySaving }) => replaySaving)).toEqual([0, 77, 79]);
        const shared = rwkvModelEstimates(usage, true, "cards_int4_notes_int8", counts);
        expect(shared.map(({ totalStateBytes }) => totalStateBytes)).toEqual([14_412_500, 2_746_500, 1_838_500]);
        expect(shared.map(({ curveLoss }) => curveLoss)).toEqual(paired.map(({ curveLoss }) => curveLoss));
        expect(shared.map(({ instantLoss }) => instantLoss)).toEqual([0, 0.25, 0.43]);
    });

    test("uses the selected precision and omits totals when counts are unavailable", () => {
        const usage = { curve: true, instant: false };
        expect(rwkvModelEstimates(usage, false, "f32", undefined).map(({ perCardAndNote }) => perCardAndNote))
            .toEqual([87_136, 13_640, 9_276]);
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
