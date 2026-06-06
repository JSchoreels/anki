// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    costWeightForAverageDr,
    dynamicDesiredRetentionEnabled,
    evaluateDynamicDesiredRetention,
    schedulingTargetDr,
    targetDrCalibration,
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

test("target calibration prefers fsrs equivalent values", () => {
    expect(targetDrCalibration([1, 2], [0.7, 0.8], [3, 4], [0.9, 0.95])).toEqual({
        weights: [3, 4],
        drs: [0.9, 0.95],
        label: "FSRS7 Eq. DR",
    });
});

test("target calibration uses average adr values without fsrs equivalents", () => {
    expect(targetDrCalibration([1, 2], [0.7, 0.8], [], [])).toEqual({
        weights: [1, 2],
        drs: [0.7, 0.8],
        label: "Avg ADR DR",
    });
});

test("scheduling target clamps to calibrated range when enabled", () => {
    expect(schedulingTargetDr(0.7, [0, 15], [0.9, 0.8], true)).toBe(0.8);
    expect(schedulingTargetDr(0.95, [0, 15], [0.9, 0.8], true)).toBe(0.9);
    expect(schedulingTargetDr(0.7, [0, 15], [0.9, 0.8], false)).toBe(0.7);
});

test("policy evaluation stays in retention range", () => {
    const value = evaluateDynamicDesiredRetention(Array(15).fill(0), 10, 5, 64, 0.75, 0.95);
    expect(value).toBeGreaterThanOrEqual(0.75);
    expect(value).toBeLessThanOrEqual(0.95);
});
