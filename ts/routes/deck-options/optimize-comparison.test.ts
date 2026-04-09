// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    deltaClass,
    formatDelta,
    formatMetric,
    formatPercentDelta,
    metricDelta,
    metricDeltaPercent,
} from "./optimize-comparison";

test("metricDelta computes new minus old", () => {
    expect(metricDelta(0.2, 0.15)).toBeCloseTo(-0.05, 10);
    expect(metricDelta(0.2, 0.25)).toBeCloseTo(0.05, 10);
});

test("deltaClass marks lower values as better", () => {
    expect(deltaClass(-0.01)).toBe("better");
    expect(deltaClass(0.01)).toBe("worse");
    expect(deltaClass(0)).toBe("equal");
});

test("format helpers use consistent precision", () => {
    expect(formatMetric(0.123456)).toBe("0.1235");
    expect(formatDelta(-0.123456)).toBe("-0.1235");
    expect(formatDelta(0.123456)).toBe("+0.1235");
    expect(formatDelta(0)).toBe("0.0000");
});

test("metricDeltaPercent computes relative change", () => {
    expect(metricDeltaPercent(2, 1.5)).toBeCloseTo(-25, 10);
    expect(metricDeltaPercent(2, 2.5)).toBeCloseTo(25, 10);
    expect(metricDeltaPercent(0, 0)).toBe(0);
    expect(metricDeltaPercent(0, 1)).toBeUndefined();
});

test("formatPercentDelta handles signs and undefined", () => {
    expect(formatPercentDelta(-12.3456)).toBe("-12.35%");
    expect(formatPercentDelta(12.3456)).toBe("+12.35%");
    expect(formatPercentDelta(0)).toBe("0.00%");
    expect(formatPercentDelta(undefined)).toBe("n/a");
});
