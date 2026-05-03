// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import type { GraphBounds } from "./graph-helpers";
import {
    type WorkloadPoint,
    renderWorkloadChart,
    SimulateWorkloadSubgraph,
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

test("renderWorkloadChart handles empty memorized data without throwing", () => {
    const svg = makeSvg();
    expect(() =>
        renderWorkloadChart(svg, bounds, [], SimulateWorkloadSubgraph.memorized),
    ).not.toThrow();
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
            start_memorized: 20,
            label: 1,
            learnSpan: 365,
        },
    ];
    delete sparse[0];
    expect(() =>
        renderWorkloadChart(svg, bounds, sparse, SimulateWorkloadSubgraph.memorized),
    ).not.toThrow();
});

test("renderWorkloadChart labels workload curves by preset name", () => {
    const svg = makeSvg();
    const points: WorkloadPoint[] = [
        {
            x: 90,
            timeCost: 100,
            count: 10,
            memorized: 50,
            start_memorized: 20,
            label: 1001,
            labelName: "Child preset",
            learnSpan: 365,
        },
    ];

    renderWorkloadChart(svg, bounds, points, SimulateWorkloadSubgraph.memorized);

    expect(svg.querySelector(".legend text")?.textContent).toBe("Child preset");
});
