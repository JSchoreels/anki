// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import type { GraphBounds } from "./graph-helpers";
import { renderWorkloadChart, SimulateWorkloadSubgraph } from "./simulator";

function makeSvg(): SVGElement {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "g")).setAttribute("class", "x-ticks");
    svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "g")).setAttribute("class", "y-ticks");
    svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "g")).setAttribute("class", "no-data");
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
    expect(() => renderWorkloadChart(svg, bounds, [], SimulateWorkloadSubgraph.memorized)).not.toThrow();
    expect(renderWorkloadChart(svg, bounds, [], SimulateWorkloadSubgraph.memorized)).toStrictEqual([]);
});
