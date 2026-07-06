// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    fsrsParamDiagnostics,
    fsrsParamsSupportSameDayEvaluation,
    fsrsSameDayEvaluationOverrideForComparison,
    OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING,
} from "./fsrs-param-diagnostics";

test("accepts default and known FSRS parameter counts", () => {
    for (const count of [0, 17, 19, 21, 34]) {
        expect(fsrsParamDiagnostics(Array(count).fill(1)).valid).toBe(true);
    }
});

test("rejects unexpected FSRS parameter counts", () => {
    const diagnostics = fsrsParamDiagnostics([1, 2, 3]);

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(false);
    expect(diagnostics.count).toBe(3);
    expect(diagnostics.outdatedFsrs7PreviewParams).toBe(false);
});

test("flags outdated 35-parameter FSRS-7 preview params", () => {
    const diagnostics = fsrsParamDiagnostics(Array(35).fill(1));

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(false);
    expect(diagnostics.outdatedFsrs7PreviewParams).toBe(true);
    expect(OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING).toContain("35 values");
    expect(OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING).toContain("34 values");
});

test("reports non-finite FSRS parameter indexes and values", () => {
    const params = Array(21).fill(1);
    params[2] = Number.NaN;
    params[5] = Number.POSITIVE_INFINITY;

    const diagnostics = fsrsParamDiagnostics(params);

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(true);
    expect(diagnostics.nonFiniteIndexes).toStrictEqual([2, 5]);
    expect(diagnostics.nonFiniteValues).toStrictEqual(["NaN", "Infinity"]);
});

test("same-day evaluation is only supported for FSRS-7 parameter sets", () => {
    expect(fsrsParamsSupportSameDayEvaluation(Array(34).fill(1))).toBe(true);

    for (const count of [0, 17, 19, 21]) {
        expect(fsrsParamsSupportSameDayEvaluation(Array(count).fill(1))).toBe(false);
    }
});

test("comparison excludes same-day targets unless both parameter sets support them", () => {
    const fsrs6Params = Array(21).fill(1);
    const fsrs7Params = Array(34).fill(1);

    expect(
        fsrsSameDayEvaluationOverrideForComparison(fsrs6Params, fsrs7Params, true),
    ).toBe(false);
    expect(
        fsrsSameDayEvaluationOverrideForComparison(fsrs7Params, fsrs7Params, true),
    ).toBe(true);
    expect(
        fsrsSameDayEvaluationOverrideForComparison(fsrs6Params, fsrs7Params, false),
    ).toBe(false);
    expect(
        fsrsSameDayEvaluationOverrideForComparison(fsrs6Params, fsrs7Params, undefined),
    ).toBeUndefined();
});
