// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    buildFailPassRatioSeries,
    buildSLineSeries,
    matrixCellIndex,
    median,
    rBucketLabel,
    sBucketLabel,
    sBucketBounds,
    seriesMinMax,
} from "./review-time-matrix";

test("matrix index is row-major by R then S", () => {
    expect(matrixCellIndex(0, 0, 3)).toBe(0);
    expect(matrixCellIndex(0, 2, 3)).toBe(2);
    expect(matrixCellIndex(1, 0, 3)).toBe(3);
    expect(matrixCellIndex(2, 1, 3)).toBe(7);
});

test("buildSLineSeries groups values by S lines over R", () => {
    // R0: [1,2], R1: [3,4], R2: [5,6]
    const values = [1, 2, 3, 4, 5, 6];
    const lines = buildSLineSeries(values, 3, 2);
    expect(lines[0]).toStrictEqual([1, 3, 5]);
    expect(lines[1]).toStrictEqual([2, 4, 6]);
});

test("buildFailPassRatioSeries computes fail/pass by S line over R", () => {
    const passValues = [10, 20, 30, 40];
    const failValues = [5, 10, 10, 20];
    const lines = buildFailPassRatioSeries(failValues, passValues, 2, 2);
    expect(lines[0]).toStrictEqual([0.5, 0.3333333333333333]);
    expect(lines[1]).toStrictEqual([0.5, 0.5]);
});

test("buildFailPassRatioSeries clamps division by zero to zero", () => {
    const lines = buildFailPassRatioSeries([10], [0], 1, 1);
    expect(lines[0][0]).toBe(0);
});

test("r bucket labels follow 5% descending bands", () => {
    expect(rBucketLabel(0)).toBe("95-100%");
    expect(rBucketLabel(1)).toBe("90-95%");
    expect(rBucketLabel(19)).toBe("0-5%");
});

test("s bucket bounds are increasing on log scale", () => {
    const [a0, b0] = sBucketBounds(0, 12);
    const [a1, b1] = sBucketBounds(1, 12);
    expect(a0).toBeLessThan(b0);
    expect(Math.abs(b0 - a1)).toBeLessThan(1e-12);
    expect(a1).toBeLessThan(b1);
});

test("s bucket label collapses when only one bucket is present", () => {
    expect(sBucketLabel(0, 1)).toBe("All S");
});

test("seriesMinMax handles equal values", () => {
    expect(seriesMinMax([[5, 5], [5]])).toStrictEqual([4, 6]);
});

test("median handles odd and even lengths", () => {
    expect(median([5, 1, 3])).toBe(3);
    expect(median([5, 1, 3, 7])).toBe(4);
    expect(median([])).toBe(0);
});
