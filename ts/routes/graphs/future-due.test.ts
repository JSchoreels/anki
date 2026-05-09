// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import { backlogCount, buildHistogram } from "./future-due";
import { GraphRange } from "./graph-helpers";

test("backlogCount sums only past due review buckets", () => {
    expect(backlogCount(new Map([[-3, 2], [-1, 4], [0, 8], [1, 16]]))).toBe(6);
});

test("buildHistogram exposes backlog count in table data", () => {
    const response = buildHistogram(
        {
            dueCounts: new Map([[-2, 5], [1, 4]]),
            haveBacklog: true,
            backlogCount: 5,
            dailyLoad: 1,
        },
        GraphRange.Month,
        false,
        () => undefined,
        false,
    );

    expect(response.tableData).toContainEqual({
        label: tr.statisticsBacklogCheckbox(),
        value: tr.statisticsReviews({ reviews: 5 }),
    });
});
