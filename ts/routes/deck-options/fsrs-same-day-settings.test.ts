// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { readFsrs7SameDaySettings, withFsrs7SameDaySettings } from "./fsrs-same-day-settings";

test("readFsrs7SameDaySettings defaults to enabled flags", () => {
    expect(readFsrs7SameDaySettings({})).toStrictEqual({
        includeSameDayReviews: true,
    });
});

test("readFsrs7SameDaySettings reads stored boolean", () => {
    expect(
        readFsrs7SameDaySettings({
            fsrs7IncludeSameDayOptimize: false,
        }),
    ).toStrictEqual({
        includeSameDayReviews: false,
    });
});

test("readFsrs7SameDaySettings falls back to legacy evaluate key", () => {
    expect(
        readFsrs7SameDaySettings({
            fsrs7IncludeSameDayEvaluate: false,
        }),
    ).toStrictEqual({
        includeSameDayReviews: false,
    });
});

test("withFsrs7SameDaySettings updates keys when values differ", () => {
    expect(
        withFsrs7SameDaySettings(
            {
                unrelated: "keep",
                fsrs7IncludeSameDayOptimize: true,
                fsrs7IncludeSameDayEvaluate: true,
            },
            {
                includeSameDayReviews: false,
            },
        ),
    ).toStrictEqual({
        unrelated: "keep",
        fsrs7IncludeSameDayOptimize: false,
    });
});

test("withFsrs7SameDaySettings migrates legacy evaluate key", () => {
    expect(
        withFsrs7SameDaySettings(
            {
                unrelated: "keep",
                fsrs7IncludeSameDayEvaluate: false,
            },
            {
                includeSameDayReviews: false,
            },
        ),
    ).toStrictEqual({
        unrelated: "keep",
        fsrs7IncludeSameDayOptimize: false,
    });
});

test("withFsrs7SameDaySettings is a no-op when values are unchanged", () => {
    expect(
        withFsrs7SameDaySettings(
            {
                fsrs7IncludeSameDayOptimize: false,
            },
            {
                includeSameDayReviews: false,
            },
        ),
    ).toBeUndefined();
});
