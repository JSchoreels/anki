import { expect, test } from "vitest";

import {
    readFsrs7SameDaySettings,
    withFsrs7SameDaySettings,
} from "./fsrs-same-day-settings";

test("readFsrs7SameDaySettings defaults to enabled flags", () => {
    expect(readFsrs7SameDaySettings({})).toStrictEqual({
        includeSameDayReviewsForOptimize: true,
        includeSameDayReviewsForEvaluate: true,
    });
});

test("readFsrs7SameDaySettings reads stored booleans", () => {
    expect(
        readFsrs7SameDaySettings({
            fsrs7IncludeSameDayOptimize: false,
            fsrs7IncludeSameDayEvaluate: true,
        }),
    ).toStrictEqual({
        includeSameDayReviewsForOptimize: false,
        includeSameDayReviewsForEvaluate: true,
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
                includeSameDayReviewsForOptimize: false,
                includeSameDayReviewsForEvaluate: true,
            },
        ),
    ).toStrictEqual({
        unrelated: "keep",
        fsrs7IncludeSameDayOptimize: false,
        fsrs7IncludeSameDayEvaluate: true,
    });
});

test("withFsrs7SameDaySettings is a no-op when values are unchanged", () => {
    expect(
        withFsrs7SameDaySettings(
            {
                fsrs7IncludeSameDayOptimize: false,
                fsrs7IncludeSameDayEvaluate: false,
            },
            {
                includeSameDayReviewsForOptimize: false,
                includeSameDayReviewsForEvaluate: false,
            },
        ),
    ).toBeUndefined();
});
