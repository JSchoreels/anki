// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { timeSpan } from "@tslib/time";
import { expect, test } from "vitest";

import { intervalPercentileSummary, IntervalRange, prepareIntervalData } from "./intervals";

function sortedIntervalsOneToHundred(): number[] {
    return Array.from({ length: 100 }, (_, i) => i + 1);
}

test("intervalPercentileSummary computes expected rounded percentiles", () => {
    const summary = intervalPercentileSummary(sortedIntervalsOneToHundred());
    expect(summary).toStrictEqual({
        p1: 2,
        p10: 11,
        p50: 51,
        p90: 90,
        p99: 99,
    });
});

test("prepareIntervalData exposes median and percentile rows for review intervals", () => {
    const intervals = sortedIntervalsOneToHundred();
    const summary = intervalPercentileSummary(intervals);
    const [_histogram, tableData] = prepareIntervalData(
        { intervals },
        IntervalRange.All,
        () => undefined,
        false,
        false,
    );

    expect(tableData).toHaveLength(5);
    expect(tableData.map((row) => row.label)).toStrictEqual([
        tableData[0].label,
        "P1 interval",
        "P10 interval",
        "P90 interval",
        "P99 interval",
    ]);
    expect(tableData.map((row) => row.value)).toStrictEqual([
        timeSpan(summary.p50 * 86400, false),
        timeSpan(summary.p1 * 86400, false),
        timeSpan(summary.p10 * 86400, false),
        timeSpan(summary.p90 * 86400, false),
        timeSpan(summary.p99 * 86400, false),
    ]);
});

test("prepareIntervalData exposes stability percentile rows in FSRS mode", () => {
    const intervals = sortedIntervalsOneToHundred();
    const [_histogram, tableData] = prepareIntervalData(
        { intervals },
        IntervalRange.All,
        () => undefined,
        false,
        true,
    );

    expect(tableData).toHaveLength(5);
    expect(tableData.slice(1).map((row) => row.label)).toStrictEqual([
        "P1 stability",
        "P10 stability",
        "P90 stability",
        "P99 stability",
    ]);
});
