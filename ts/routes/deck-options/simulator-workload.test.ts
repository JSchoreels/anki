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
    });
    const config = new DeckConfig({
        name: 'Preset "A"',
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
    expect(request.params).toStrictEqual([1, 2, 3]);
    expect(request.search).toBe('deck:"Default" preset:"Preset \\"A\\"" -is:suspended');
});
