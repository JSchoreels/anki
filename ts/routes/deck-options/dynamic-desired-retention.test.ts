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
        fixedTarget: false,
    });
});

test("target calibration prefers fixed target values", () => {
    expect(
        targetDrCalibration([1, 2], [0.7, 0.8], [3, 4], [0.9, 0.95], [16, 4], [0.8, 0.9]),
    ).toEqual({
        weights: [16, 4],
        drs: [0.8, 0.9],
        label: "Fixed target DR",
        fixedTarget: true,
    });
});

test("target calibration uses average adr values without fsrs equivalents", () => {
    expect(targetDrCalibration([1, 2], [0.7, 0.8], [], [])).toEqual({
        weights: [1, 2],
        drs: [0.7, 0.8],
        label: "Avg ADR DR",
        fixedTarget: false,
    });
});

test("fixed target cost weight uses next covered point", () => {
    expect(costWeightForAverageDr(0.79, [64, 16], [0.8, 0.9], true)).toBe(64);
    expect(costWeightForAverageDr(0.85, [64, 16], [0.8, 0.9], true)).toBe(16);
    expect(costWeightForAverageDr(0.91, [64, 16], [0.8, 0.9], true)).toBe(null);
});

test("scheduling target clamps to calibrated range when enabled", () => {
    expect(schedulingTargetDr(0.7, [0, 15], [0.9, 0.8], true)).toBe(0.8);
    expect(schedulingTargetDr(0.95, [0, 15], [0.9, 0.8], true)).toBe(0.9);
    expect(schedulingTargetDr(0.7, [0, 15], [0.9, 0.8], false)).toBe(0.7);
    expect(schedulingTargetDr(0.95, [64, 16], [0.8, 0.9], true, true, 0.75)).toBe(0.9);
    expect(schedulingTargetDr(0.7, [64, 16], [0.8, 0.9], true, true, 0.75)).toBe(0.75);
});

test("policy evaluation stays in retention range", () => {
    const value = evaluateDynamicDesiredRetention(Array(15).fill(0), 10, 5, 64, 0.75, 0.95);
    expect(value).toBeGreaterThanOrEqual(0.75);
    expect(value).toBeLessThanOrEqual(0.95);
});
