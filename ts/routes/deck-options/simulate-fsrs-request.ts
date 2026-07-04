// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { DeckConfig_Config } from "@generated/anki/deck_config_pb";
import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";

import {
    HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
} from "./help-me-decide-defaults";

export function buildSimulateFsrsRequest({
    config,
    params,
    search,
    newCardsIgnoreReviewLimit,
    reviewFuzzEnabled,
    reviewFuzzBase,
    reviewFuzzFactorShort,
    reviewFuzzFactorMid,
    reviewFuzzFactorLong,
}: {
    config: DeckConfig_Config;
    params: number[];
    search: string;
    newCardsIgnoreReviewLimit: boolean;
    reviewFuzzEnabled: boolean;
    reviewFuzzBase: number;
    reviewFuzzFactorShort: number;
    reviewFuzzFactorMid: number;
    reviewFuzzFactorLong: number;
}): SimulateFsrsReviewRequest {
    return new SimulateFsrsReviewRequest({
        params,
        desiredRetention: config.desiredRetention,
        newLimit: config.newPerDay,
        reviewLimit: config.reviewsPerDay,
        maxInterval: config.maximumReviewInterval,
        search,
        newCardsIgnoreReviewLimit,
        easyDaysPercentages: config.easyDaysPercentages,
        reviewOrder: config.reviewOrder,
        historicalRetention: config.historicalRetention,
        learningStepCount: config.learnSteps.length,
        relearningStepCount: config.relearnSteps.length,
        reviewFuzzBase: reviewFuzzEnabled ? reviewFuzzBase : 0,
        reviewFuzzFactorShort: reviewFuzzEnabled ? reviewFuzzFactorShort : 0,
        reviewFuzzFactorMid: reviewFuzzEnabled ? reviewFuzzFactorMid : 0,
        reviewFuzzFactorLong: reviewFuzzEnabled ? reviewFuzzFactorLong : 0,
        helpMeDecideTransitionBlendAlpha: HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
        helpMeDecideEnforceMonotonicSuccessGradeProbs: HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
        fsrsDynamicDesiredRetentionParams: config.fsrsDynamicDesiredRetentionParams,
        fsrsDynamicDesiredRetentionWeights: config.fsrsDynamicDesiredRetentionWeights,
        fsrsDynamicDesiredRetentionAvgDrs: config.fsrsDynamicDesiredRetentionAvgDrs,
        fsrsDynamicDesiredRetentionFsrsEqWeights: config.fsrsDynamicDesiredRetentionFsrsEqWeights,
        fsrsDynamicDesiredRetentionFsrsEqDrs: config.fsrsDynamicDesiredRetentionFsrsEqDrs,
        fsrsDynamicDesiredRetentionFixedTargetWeights: config.fsrsDynamicDesiredRetentionFixedTargetWeights,
        fsrsDynamicDesiredRetentionFixedTargetDrs: config.fsrsDynamicDesiredRetentionFixedTargetDrs,
        fsrsDynamicDesiredRetentionMin: config.fsrsDynamicDesiredRetentionMin,
        fsrsDynamicDesiredRetentionMax: config.fsrsDynamicDesiredRetentionMax,
        fsrsDynamicDesiredRetentionClamp: config.fsrsDynamicDesiredRetentionClamp,
    });
}
