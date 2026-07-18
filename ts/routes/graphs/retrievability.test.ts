// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    GraphsResponse,
    GraphsResponse_Retrievability,
    GraphsResponse_Retrievability_Series,
} from "@generated/anki/stats_pb";
import { expect, test } from "vitest";

import { shouldShowRetrievabilityGraph } from "./retrievability";

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
