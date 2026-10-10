// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { defaultScheduleCopyOptions, withCopiedSchedule, withCopiedScheduleAuxData } from "./apply-schedule";
import { dueDateScheduler, scheduler } from "./scheduler-choice";

test("each schedule category can be copied independently", () => {
    const source = new DeckConfig_Config({
        rwkvReviewInstantOrderEnabled: true,
        fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
        desiredRetention: 0.9,
        fsrsParams7: Array(34).fill(2),
        rwkvReviewMinElapsedSecs: 60,
        maximumReviewInterval: 1000,
        newPerDay: 50,
    });
    const target = new DeckConfig_Config({
        rwkvReviewEnabled: true,
        desiredRetention: 0.8,
        fsrsParams7: Array(34).fill(1),
        rwkvReviewMinElapsedSecs: 10,
        maximumReviewInterval: 500,
        newPerDay: 20,
    });
    for (const copyScheduler of [false, true]) {
        for (const fallback of [false, true]) {
            for (const settings of [false, true]) {
                for (const retention of [false, true]) {
                    const updated = withCopiedSchedule(target, source, {
                        scheduler: copyScheduler,
                        fallback,
                        settings,
                        retention,
                    });
                    expect(scheduler(updated)).toBe(copyScheduler ? "rwkv-instant" : "rwkv-curve");
                    expect(dueDateScheduler(updated)).toBe(copyScheduler && fallback ? "fsrs-7" : "rwkv-curve");
                    expect(updated.desiredRetention).toBe(retention ? 0.9 : 0.8);
                    expect(updated.fsrsParams7).toEqual(Array(34).fill(settings ? 2 : 1));
                    expect(updated.rwkvReviewMinElapsedSecs).toBe(settings ? 60 : 10);
                    expect(updated.maximumReviewInterval).toBe(settings ? 1000 : 500);
                    expect(updated.newPerDay).toBe(20);
                }
            }
        }
    }
    expect(target.rwkvReviewInstantOrderEnabled).toBe(false);
    expect(target.fsrsParams7).toEqual(Array(34).fill(1));
});

test("fallback alone changes Instant due dates without replacing the review model", () => {
    const source = new DeckConfig_Config({ fsrsVersion: DeckConfig_Config_FsrsVersion.SIX });
    const options = { scheduler: false, fallback: true, settings: false, retention: false };
    const instant = new DeckConfig_Config({ rwkvReviewInstantOrderEnabled: true, rwkvReviewEnabled: true });
    const updated = withCopiedSchedule(instant, source, options);
    expect(scheduler(updated)).toBe("rwkv-instant");
    expect(dueDateScheduler(updated)).toBe("fsrs-6");

    const curve = new DeckConfig_Config({ rwkvReviewEnabled: true });
    expect(withCopiedSchedule(curve, source, options)).toEqual(curve);
    const fsrs = new DeckConfig_Config({ fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN });
    expect(withCopiedSchedule(fsrs, source, options)).toEqual(fsrs);
});

test("copying an interval review scheduler changes its due-date model and disables Instant", () => {
    const target = new DeckConfig_Config({ rwkvReviewInstantOrderEnabled: true, rwkvReviewEnabled: true });
    const options = { scheduler: true, fallback: false, settings: false, retention: false };
    const fsrs = withCopiedSchedule(
        target,
        new DeckConfig_Config({ fsrsVersion: DeckConfig_Config_FsrsVersion.SIX }),
        options,
    );
    expect(scheduler(fsrs)).toBe("fsrs-6");
    expect(dueDateScheduler(fsrs)).toBe("fsrs-6");
    const curve = withCopiedSchedule(target, new DeckConfig_Config({ rwkvReviewEnabled: true }), options);
    expect(scheduler(curve)).toBe("rwkv-curve");
    expect(curve.rwkvReviewInstantOrderEnabled).toBe(false);
});

test("copied model parameter arrays can be edited independently", () => {
    const source = new DeckConfig_Config({ fsrsParams7: Array(34).fill(1) });
    const updated = withCopiedSchedule(new DeckConfig_Config(), source, defaultScheduleCopyOptions());
    updated.fsrsParams7[0] = 2;
    expect(source.fsrsParams7[0]).toBe(1);
});

test("copying auxiliary model settings preserves add-on data and resets absent options to their defaults", () => {
    const target = {
        fsrs7IncludeSameDayEvaluate: false,
        fsrs7EnableSchedulingPenalties: true,
        fsrsEvaluationSearch: "deck:Old",
        addon: { enabled: true },
    };
    expect(withCopiedScheduleAuxData(target, {})).toEqual({
        fsrs7IncludeSameDayOptimize: true,
        fsrs7EnableSchedulingPenalties: false,
        fsrsEvaluationSearch: "",
        addon: { enabled: true },
    });
    expect(withCopiedScheduleAuxData({}, {
        fsrs7IncludeSameDayOptimize: false,
        fsrs7EnableSchedulingPenalties: true,
        fsrsEvaluationSearch: "tag:study",
        addon: { enabled: false },
    })).toEqual({
        fsrs7IncludeSameDayOptimize: false,
        fsrs7EnableSchedulingPenalties: true,
        fsrsEvaluationSearch: "tag:study",
    });
});
