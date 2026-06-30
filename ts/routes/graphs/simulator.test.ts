// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import type { GraphBounds } from "./graph-helpers";
import {
    centeredMovingAverage,
    renderWorkloadChart,
    SimulateWorkloadSubgraph,
    smoothPointsByLabel,
    type WorkloadPoint,
    workloadSameMemorizedSavings,
} from "./simulator";

function makeSvg(): SVGElement {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.appendChild(
        document.createElementNS("http://www.w3.org/2000/svg", "g"),
    ).setAttribute("class", "x-ticks");
    svg.appendChild(
        document.createElementNS("http://www.w3.org/2000/svg", "g"),
    ).setAttribute("class", "y-ticks");
    svg.appendChild(
        document.createElementNS("http://www.w3.org/2000/svg", "g"),
    ).setAttribute("class", "no-data");
    return svg;
}

const bounds: GraphBounds = {
    width: 600,
    height: 250,
    marginLeft: 70,
    marginRight: 70,
    marginTop: 20,
    marginBottom: 25,
};

function workloadPoint(
    labelName: string,
    memorized: number,
    timeCost: number,
): WorkloadPoint {
    return {
        x: memorized,
        timeCost,
        count: 10,
        memorized,
        weightedMemorized: memorized,
        reviewless_end_memorized: 0,
        reviewless_end_weighted_memorized: 0,
        label: labelName.includes("ADR") ? 2 : 1,
        labelName,
        learnSpan: 365,
    };
}

test("renderWorkloadChart handles empty memorized data without throwing", () => {
    const svg = makeSvg();
    expect(() => renderWorkloadChart(svg, bounds, [], SimulateWorkloadSubgraph.memorized)).not.toThrow();
    expect(
        renderWorkloadChart(svg, bounds, [], SimulateWorkloadSubgraph.memorized),
    ).toStrictEqual([]);
});

test("renderWorkloadChart handles sparse workload data without throwing", () => {
    const svg = makeSvg();
    const sparse: WorkloadPoint[] = [
        {
            x: 90,
            timeCost: 100,
            count: 10,
            memorized: 50,
            weightedMemorized: 12,
            reviewless_end_memorized: 20,
            reviewless_end_weighted_memorized: 8,
            label: 1,
            learnSpan: 365,
        },
    ];
    delete sparse[0];
    expect(() => renderWorkloadChart(svg, bounds, sparse, SimulateWorkloadSubgraph.memorized)).not.toThrow();
});

test("renderWorkloadChart labels workload curves by preset name", () => {
    const svg = makeSvg();
    const points: WorkloadPoint[] = [
        {
            x: 90,
            timeCost: 100,
            count: 10,
            memorized: 50,
            weightedMemorized: 12,
            reviewless_end_memorized: 20,
            reviewless_end_weighted_memorized: 8,
            label: 1001,
            labelName: "Child preset",
            learnSpan: 365,
        },
    ];

    renderWorkloadChart(svg, bounds, points, SimulateWorkloadSubgraph.memorized);

    expect(svg.querySelector(".legend text")?.textContent).toBe("Child preset");
});

test("renderWorkloadChart handles weighted workload metrics", () => {
    const points: WorkloadPoint[] = [
        {
            x: 90,
            timeCost: 100,
            count: 10,
            memorized: 50,
            weightedMemorized: 12,
            reviewless_end_memorized: 20,
            reviewless_end_weighted_memorized: 8,
            label: 1001,
            labelName: "Child preset",
            learnSpan: 365,
        },
    ];

    expect(() =>
        renderWorkloadChart(
            makeSvg(),
            bounds,
            points,
            SimulateWorkloadSubgraph.weightedMemorized,
        )
    ).not.toThrow();
    expect(() =>
        renderWorkloadChart(
            makeSvg(),
            bounds,
            points,
            SimulateWorkloadSubgraph.weightedRatio,
        )
    ).not.toThrow();
});

test("centeredMovingAverage smooths without lagging the series", () => {
    expect(centeredMovingAverage([10, 100, 10], 3)).toStrictEqual([55, 40, 55]);
    expect(centeredMovingAverage([10, 100, 10], 1)).toStrictEqual([10, 100, 10]);
});

test("smoothPointsByLabel sorts and smooths each simulation separately", () => {
    const points: WorkloadPoint[] = [
        {
            x: 3,
            timeCost: 10,
            count: 10,
            memorized: 10,
            weightedMemorized: 10,
            reviewless_end_memorized: 0,
            reviewless_end_weighted_memorized: 0,
            label: 1,
            learnSpan: 365,
        },
        {
            x: 1,
            timeCost: 10,
            count: 10,
            memorized: 10,
            weightedMemorized: 10,
            reviewless_end_memorized: 0,
            reviewless_end_weighted_memorized: 0,
            label: 1,
            learnSpan: 365,
        },
        {
            x: 2,
            timeCost: 100,
            count: 100,
            memorized: 100,
            weightedMemorized: 100,
            reviewless_end_memorized: 0,
            reviewless_end_weighted_memorized: 0,
            label: 1,
            learnSpan: 365,
        },
        {
            x: 1,
            timeCost: 1000,
            count: 1000,
            memorized: 1000,
            weightedMemorized: 1000,
            reviewless_end_memorized: 0,
            reviewless_end_weighted_memorized: 0,
            label: 2,
            learnSpan: 365,
        },
    ];

    const smoothed = smoothPointsByLabel(points, 3);

    expect(smoothed.map((point) => [point.label, point.x])).toStrictEqual([
        [1, 1],
        [1, 2],
        [1, 3],
        [2, 1],
    ]);
    expect(smoothed.slice(0, 3).map((point) => point.memorized)).toStrictEqual([
        55,
        40,
        55,
    ]);
    expect(smoothed[3].memorized).toBe(1000);
});

test("workloadSameMemorizedSavings compares ADR cost against fixed DR memory targets", () => {
    const table = workloadSameMemorizedSavings([
        workloadPoint("Yomitan (Fixed DR)", 50, 100),
        workloadPoint("Yomitan (Fixed DR)", 80, 200),
        workloadPoint("Yomitan (ADR)", 50, 80),
        workloadPoint("Yomitan (ADR)", 80, 160),
    ]);

    expect(table).toHaveLength(2);
    expect(table[0].label).toBe("ADR same-memorized saving");
    expect(table[0].value).toContain("20.0%");
    expect(table[0].value).toContain("2/2");
});

test("workloadSameMemorizedSavings pairs nested preset workload labels", () => {
    const table = workloadSameMemorizedSavings([
        workloadPoint("Young cards (Yomitan (Fixed DR))", 60, 100),
        workloadPoint("Young cards (Yomitan (ADR))", 60, 90),
    ]);

    expect(table[0].value).toContain("10.0%");
});
