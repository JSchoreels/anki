// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    costWeightForAverageDr,
    dynamicDesiredRetentionEnabled,
    evaluateDynamicDesiredRetention,
} from "./dynamic-desired-retention";

test("dynamic desired retention requires params and calibration", () => {
    expect(
        dynamicDesiredRetentionEnabled({
            fsrsDynamicDesiredRetentionEnabled: true,
            fsrsDynamicDesiredRetentionParams: [],
            fsrsDynamicDesiredRetentionWeights: [0, 15],
            fsrsDynamicDesiredRetentionAvgDrs: [0.9, 0.8],
            fsrsDynamicDesiredRetentionMin: 0.75,
            fsrsDynamicDesiredRetentionMax: 0.95,
        }),
    ).toBe(false);
});

test("cost weight interpolation uses log weight", () => {
    expect(costWeightForAverageDr(0.85, [0, 15], [0.9, 0.8])).toBeCloseTo(3);
});

test("policy evaluation stays in retention range", () => {
    const value = evaluateDynamicDesiredRetention(Array(15).fill(0), 10, 5, 64, 0.75, 0.95);
    expect(value).toBeGreaterThanOrEqual(0.75);
    expect(value).toBeLessThanOrEqual(0.95);
});
