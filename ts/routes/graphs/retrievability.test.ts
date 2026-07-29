// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    GraphsResponse,
    GraphsResponse_Retrievability,
    GraphsResponse_Retrievability_Series,
} from "@generated/anki/stats_pb";
import { expect, test } from "vitest";

import type { GraphData } from "./retrievability";
import { prepareData, shouldShowRetrievabilityGraph } from "./retrievability";

test("retrievability graph is shown when RWKV data exists without FSRS", () => {
    const sourceData = new GraphsResponse({
        fsrs: false,
        retrievability: new GraphsResponse_Retrievability({
            rwkv: new GraphsResponse_Retrievability_Series({
                retrievability: { 75: 1 },
            }),
        }),
    });

    expect(shouldShowRetrievabilityGraph(sourceData)).toBe(true);
});

test("retrievability graph remains available for FSRS without scored cards", () => {
    expect(shouldShowRetrievabilityGraph(new GraphsResponse({ fsrs: true }))).toBe(true);
});

test("retrievability graph stays hidden when neither FSRS nor RWKV is active", () => {
    expect(shouldShowRetrievabilityGraph(new GraphsResponse())).toBe(false);
});

function graphData(rwkv: boolean): GraphData {
    const fsrs = {
        retrievability: new Map([[75, 1]]),
        average: 75,
        sumByCard: 0.75,
        sumByNote: 0.75,
    };

    return {
        active: fsrs,
        fsrs,
        rwkv: rwkv ? fsrs : null,
    };
}

function clickQuery(rwkv: boolean, shiftKey: boolean): string {
    let query = "";
    const [histogram] = prepareData(
        graphData(rwkv),
        (_type, detail) => {
            query = detail.query;
        },
        true,
    );
    const bin = histogram!.series[0].bins.find((bin) => bin.length)!;
    histogram!.onClick!(bin, shiftKey);
    return query;
}

test("retrievability graph searches RWKV on an ordinary click when available", () => {
    expect(clickQuery(true, false)).toBe(
        "\"prop:rwkv:r>=0.75\" AND \"prop:rwkv:r<0.8\"",
    );
});

test("retrievability graph searches FSRS on shift-click", () => {
    expect(clickQuery(true, true)).toBe("\"prop:r>=0.75\" AND \"prop:r<0.8\"");
});

test("retrievability graph searches FSRS on an ordinary click without RWKV", () => {
    expect(clickQuery(false, false)).toBe("\"prop:r>=0.75\" AND \"prop:r<0.8\"");
});
