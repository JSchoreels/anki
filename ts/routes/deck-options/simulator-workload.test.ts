// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    DeckConfig,
    DeckConfig_Config_FsrsVersion,
    DeckConfig_Config_ReviewCardOrder,
} from "@generated/anki/deck_config_pb";
import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";
import { expect, test } from "vitest";

import { workloadRequestForPreset } from "./simulator-workload";

test("workload request keeps Help Me Decide menu settings", () => {
    const baseRequest = new SimulateFsrsReviewRequest({
        newLimit: 11,
        reviewOrder: DeckConfig_Config_ReviewCardOrder.RANDOM,
        reviewLimit: 9999,
        maxInterval: 456,
        easyDaysPercentages: [1, 2, 3, 4, 5, 6, 7],
        reviewFuzzBase: 2,
        reviewFuzzFactorShort: 0.3,
        reviewFuzzFactorMid: 0.2,
        reviewFuzzFactorLong: 0.1,
    });
    const config = new DeckConfig({
        name: "Preset \"A\"",
        config: {
            fsrsVersion: DeckConfig_Config_FsrsVersion.SIX,
            fsrsParams6: [1, 2, 3],
            desiredRetention: 0.91,
            newPerDay: 25,
            maximumReviewInterval: 123,
            reviewOrder: DeckConfig_Config_ReviewCardOrder.DAY_THEN_DECK,
            easyDaysPercentages: [0, 0, 0, 0, 0, 0, 0],
            historicalRetention: 0.88,
            learnSteps: [1, 10],
            relearnSteps: [10],
        },
    });

    const request = workloadRequestForPreset(baseRequest, "Default", config);

    expect(request.reviewOrder).toBe(DeckConfig_Config_ReviewCardOrder.RANDOM);
    expect(request.newLimit).toBe(11);
    expect(request.reviewLimit).toBe(9999);
    expect(request.maxInterval).toBe(456);
    expect(request.easyDaysPercentages).toStrictEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(request.reviewFuzzBase).toBe(2);
    expect(request.reviewFuzzFactorShort).toBe(0.3);
    expect(request.reviewFuzzFactorMid).toBe(0.2);
    expect(request.reviewFuzzFactorLong).toBe(0.1);
    expect(request.params).toStrictEqual([1, 2, 3]);
    expect(request.search).toBe("deck:\"Default\" preset:\"Preset \\\"A\\\"\" -is:suspended");
});

test("workload request enables ADR for dynamic presets", () => {
    const baseRequest = new SimulateFsrsReviewRequest({
        simulateDynamicDesiredRetention: true,
    });
    const config = new DeckConfig({
        name: "Dynamic",
        config: {
            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
            fsrsParams7: [1, 2, 3],
            desiredRetention: 0.9,
            fsrsDynamicDesiredRetentionEnabled: true,
            fsrsDynamicDesiredRetentionParams: Array(15).fill(0),
            fsrsDynamicDesiredRetentionWeights: [0, 15],
            fsrsDynamicDesiredRetentionAvgDrs: [0.8, 0.9],
            fsrsDynamicDesiredRetentionFixedTargetWeights: [64, 16],
            fsrsDynamicDesiredRetentionFixedTargetDrs: [0.8, 0.9],
            fsrsDynamicDesiredRetentionMin: 0.75,
            fsrsDynamicDesiredRetentionMax: 0.95,
        },
    });

    const request = workloadRequestForPreset(baseRequest, "Default", config);

    expect(request.simulateDynamicDesiredRetention).toBe(true);
    expect(request.params).toStrictEqual([1, 2, 3]);
    expect(request.fsrsDynamicDesiredRetentionParams).toHaveLength(15);
    expect(request.fsrsDynamicDesiredRetentionWeights).toStrictEqual([0, 15]);
    expect(request.fsrsDynamicDesiredRetentionAvgDrs).toStrictEqual([0.8, 0.9]);
    expect(request.fsrsDynamicDesiredRetentionFixedTargetWeights).toStrictEqual([64, 16]);
    expect(request.fsrsDynamicDesiredRetentionFixedTargetDrs).toStrictEqual([0.8, 0.9]);
    expect(request.fsrsDynamicDesiredRetentionMin).toBe(0.75);
    expect(request.fsrsDynamicDesiredRetentionMax).toBe(0.95);
});

test("workload request disables ADR when Help Me Decide toggle is off", () => {
    const baseRequest = new SimulateFsrsReviewRequest({
        simulateDynamicDesiredRetention: false,
    });
    const config = new DeckConfig({
        name: "Dynamic",
        config: {
            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
            fsrsParams7: [1, 2, 3],
            desiredRetention: 0.9,
            fsrsDynamicDesiredRetentionEnabled: true,
            fsrsDynamicDesiredRetentionParams: Array(15).fill(0),
            fsrsDynamicDesiredRetentionWeights: [0, 15],
            fsrsDynamicDesiredRetentionAvgDrs: [0.8, 0.9],
            fsrsDynamicDesiredRetentionFixedTargetWeights: [64, 16],
            fsrsDynamicDesiredRetentionFixedTargetDrs: [0.8, 0.9],
            fsrsDynamicDesiredRetentionMin: 0.75,
            fsrsDynamicDesiredRetentionMax: 0.95,
        },
    });

    const request = workloadRequestForPreset(baseRequest, "Default", config);

    expect(request.simulateDynamicDesiredRetention).toBe(false);
});

test("workload request preserves ADR flag for FSRS7 overlay routes", () => {
    const baseRequest = new SimulateFsrsReviewRequest({
        simulateDynamicDesiredRetention: true,
    });
    const config = new DeckConfig({
        name: "Overlay",
        config: {
            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
            fsrsParams7: [1, 2, 3],
            desiredRetention: 0.9,
        },
    });

    const request = workloadRequestForPreset(baseRequest, "Default", config);

    expect(request.simulateDynamicDesiredRetention).toBe(true);
});
