// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { dueDateScheduler, scheduler, withDueDateScheduler, withScheduler } from "./scheduler-choice";

test("existing Instant presets keep their selected due-date scheduler", () => {
    for (const curveEnabled of [false, true]) {
        const config = new DeckConfig_Config({
            fsrsVersion: DeckConfig_Config_FsrsVersion.SIX,
            rwkvReviewEnabled: curveEnabled,
            rwkvReviewInstantOrderEnabled: true,
        });
        expect(scheduler(config)).toBe("rwkv-instant");
        expect(dueDateScheduler(config)).toBe(curveEnabled ? "rwkv-curve" : "fsrs-6");
    }
});

test("choosing an FSRS scheduler disables both RWKV overrides and preserves other model parameters", () => {
    const config = new DeckConfig_Config({
        rwkvReviewEnabled: true,
        rwkvReviewInstantOrderEnabled: true,
        fsrsParams6: Array(21).fill(1),
        fsrsParams7: Array(34).fill(2),
        rwkvReviewMinInterveningReviews: 3,
    });
    for (const choice of ["fsrs-6", "fsrs-7"] as const) {
        const updated = withScheduler(config, choice);
        expect(updated.fsrsVersion).toBe(
            choice === "fsrs-6"
                ? DeckConfig_Config_FsrsVersion.SIX
                : DeckConfig_Config_FsrsVersion.SEVEN,
        );
        expect(updated.rwkvReviewEnabled).toBe(false);
        expect(updated.rwkvReviewInstantOrderEnabled).toBe(false);
        expect(updated.fsrsParams6).toEqual(config.fsrsParams6);
        expect(updated.fsrsParams7).toEqual(config.fsrsParams7);
        expect(updated.rwkvReviewMinInterveningReviews).toBe(3);
    }
    expect(config.rwkvReviewEnabled).toBe(true);
    expect(config.rwkvReviewInstantOrderEnabled).toBe(true);
});

test("switching between Curve and Instant keeps Curve due dates until another companion is selected", () => {
    const config = new DeckConfig_Config({ fsrsVersion: DeckConfig_Config_FsrsVersion.SIX });
    const curve = withScheduler(config, "rwkv-curve");
    expect(curve.rwkvReviewEnabled).toBe(true);
    expect(curve.rwkvReviewInstantOrderEnabled).toBe(false);
    const instant = withScheduler(curve, "rwkv-instant");
    expect(instant.rwkvReviewEnabled).toBe(true);
    expect(instant.rwkvReviewInstantOrderEnabled).toBe(true);
    const fsrs = withDueDateScheduler(instant, "fsrs-7");
    expect(fsrs.rwkvReviewEnabled).toBe(false);
    expect(fsrs.rwkvReviewInstantOrderEnabled).toBe(true);
    expect(fsrs.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SEVEN);
});

test("FSRS versions on older presets are read without rewriting their configuration", () => {
    for (const version of [DeckConfig_Config_FsrsVersion.FOUR, DeckConfig_Config_FsrsVersion.FIVE]) {
        const config = new DeckConfig_Config({ fsrsVersion: version });
        expect(scheduler(config)).toBe(version === DeckConfig_Config_FsrsVersion.FOUR ? "fsrs-4" : "fsrs-5");
        expect(config.fsrsVersion).toBe(version);
    }
});
