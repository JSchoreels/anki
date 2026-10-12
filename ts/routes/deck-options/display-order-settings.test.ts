// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { readDisplayOrderSettings, withDisplayOrderSettings } from "./display-order-settings";

test("intraday priority defaults off for absent or invalid metadata", () => {
    for (
        const auxData of [
            {},
            { "jschoreels.deckoptions": null },
            { "jschoreels.deckoptions": { intraday_learning_before_reviews: "true" } },
        ]
    ) {
        expect(readDisplayOrderSettings(auxData).intradayLearningBeforeReviews).toBe(false);
    }
    expect(withDisplayOrderSettings({}, { intradayLearningBeforeReviews: false })).toBeUndefined();
});

test("editing intraday priority preserves other display options and fork metadata", () => {
    const original = {
        "jschoreels.fsrs": { fsrs_version: 1 },
        "jschoreels.rwkv": { rwkv_review_enabled: true },
        "jschoreels.deckoptions": { future_option: "keep" },
        addon: { value: 42 },
    };
    const enabled = withDisplayOrderSettings(original, { intradayLearningBeforeReviews: true })!;
    expect(enabled).toEqual({
        ...original,
        "jschoreels.deckoptions": { future_option: "keep", intraday_learning_before_reviews: true },
    });
    expect(readDisplayOrderSettings(enabled).intradayLearningBeforeReviews).toBe(true);
    const disabled = withDisplayOrderSettings(enabled, { intradayLearningBeforeReviews: false })!;
    expect(disabled).toEqual({
        ...original,
        "jschoreels.deckoptions": { future_option: "keep", intraday_learning_before_reviews: false },
    });
    expect(readDisplayOrderSettings(disabled).intradayLearningBeforeReviews).toBe(false);
    expect(original["jschoreels.deckoptions"]).toEqual({ future_option: "keep" });
    expect(withDisplayOrderSettings(disabled, { intradayLearningBeforeReviews: false })).toBeUndefined();
});
