// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config_ReviewMix as ReviewMix } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { readDisplayOrderSettings, withDisplayOrderSettings } from "./display-order-settings";

test("intraday order defaults to mixed for absent or invalid metadata", () => {
    for (
        const auxData of [
            {},
            { "jschoreels.deckoptions": null },
            { "jschoreels.deckoptions": { intraday_learning_mix: "before" } },
            { "jschoreels.deckoptions": { intraday_learning_mix: 99 } },
        ]
    ) {
        expect(readDisplayOrderSettings(auxData).intradayLearningMix).toBe(ReviewMix.MIX_WITH_REVIEWS);
    }
    expect(withDisplayOrderSettings({}, { intradayLearningMix: ReviewMix.MIX_WITH_REVIEWS })).toBeUndefined();
});

test("all intraday orders preserve other display options and fork metadata", () => {
    const original = {
        "jschoreels.fsrs": { fsrs_version: 1 },
        "jschoreels.rwkv": { rwkv_review_enabled: true },
        "jschoreels.deckoptions": { future_option: "keep" },
        addon: { value: 42 },
    };
    let current: Record<string, unknown> = original;
    for (const mix of [ReviewMix.BEFORE_REVIEWS, ReviewMix.AFTER_REVIEWS, ReviewMix.MIX_WITH_REVIEWS]) {
        const updated = withDisplayOrderSettings(current, { intradayLearningMix: mix })!;
        expect(updated).toEqual({
            ...original,
            "jschoreels.deckoptions": { future_option: "keep", intraday_learning_mix: mix },
        });
        expect(readDisplayOrderSettings(updated).intradayLearningMix).toBe(mix);
        expect(withDisplayOrderSettings(updated, { intradayLearningMix: mix })).toBeUndefined();
        current = updated;
    }
    expect(original["jschoreels.deckoptions"]).toEqual({ future_option: "keep" });
});

test("the old enabled toggle migrates to Before without losing other metadata", () => {
    const original = {
        "jschoreels.deckoptions": { future_option: "keep", intraday_learning_before_reviews: true },
    };
    expect(readDisplayOrderSettings(original).intradayLearningMix).toBe(ReviewMix.BEFORE_REVIEWS);
    expect(withDisplayOrderSettings(original, { intradayLearningMix: ReviewMix.BEFORE_REVIEWS })).toEqual({
        "jschoreels.deckoptions": { future_option: "keep", intraday_learning_mix: ReviewMix.BEFORE_REVIEWS },
    });
    expect(
        readDisplayOrderSettings({
            "jschoreels.deckoptions": {
                intraday_learning_before_reviews: true,
                intraday_learning_mix: ReviewMix.AFTER_REVIEWS,
            },
        }).intradayLearningMix,
    ).toBe(ReviewMix.AFTER_REVIEWS);
});
