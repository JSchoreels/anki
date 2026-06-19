// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { type DeckConfig, type DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";
import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";

import { escapeSearchText } from "./lib";

function selectedFsrsParams(config: DeckConfig_Config): number[] {
    switch (config.fsrsVersion) {
        case DeckConfig_Config_FsrsVersion.SIX:
            return config.fsrsParams6;
        case DeckConfig_Config_FsrsVersion.FIVE:
            return config.fsrsParams5;
        case DeckConfig_Config_FsrsVersion.FOUR:
            return config.fsrsParams4;
        default:
            return config.fsrsParams7;
    }
}

function supportsDynamicDesiredRetentionSimulation(config: DeckConfig_Config): boolean {
    return config.fsrsVersion === DeckConfig_Config_FsrsVersion.SEVEN;
}

function workloadSearchForPreset(
    deckNameForSearch: string,
    presetName: string,
): string {
    return `deck:"${deckNameForSearch}" preset:"${escapeSearchText(presetName)}" -is:suspended`;
}

export function workloadRequestForPreset(
    baseRequest: SimulateFsrsReviewRequest,
    deckNameForSearch: string,
    config: DeckConfig,
): SimulateFsrsReviewRequest {
    const inner = config.config!;
    const request = new SimulateFsrsReviewRequest(baseRequest);
    request.params = selectedFsrsParams(inner);
    request.desiredRetention = inner.desiredRetention;
    request.search = workloadSearchForPreset(deckNameForSearch, config.name);
    request.workloadPresetLabel = config.name;
    request.historicalRetention = inner.historicalRetention;
    request.learningStepCount = inner.learnSteps.length;
    request.relearningStepCount = inner.relearnSteps.length;
    request.simulateDynamicDesiredRetention = baseRequest.simulateDynamicDesiredRetention
        && supportsDynamicDesiredRetentionSimulation(inner);
    request.fsrsDynamicDesiredRetentionParams = [
        ...inner.fsrsDynamicDesiredRetentionParams,
    ];
    request.fsrsDynamicDesiredRetentionWeights = [
        ...inner.fsrsDynamicDesiredRetentionWeights,
    ];
    request.fsrsDynamicDesiredRetentionAvgDrs = [
        ...inner.fsrsDynamicDesiredRetentionAvgDrs,
    ];
    request.fsrsDynamicDesiredRetentionFsrsEqWeights = [
        ...inner.fsrsDynamicDesiredRetentionFsrsEqWeights,
    ];
    request.fsrsDynamicDesiredRetentionFsrsEqDrs = [
        ...inner.fsrsDynamicDesiredRetentionFsrsEqDrs,
    ];
    request.fsrsDynamicDesiredRetentionFixedTargetWeights = [
        ...(inner.fsrsDynamicDesiredRetentionFixedTargetWeights ?? []),
    ];
    request.fsrsDynamicDesiredRetentionFixedTargetDrs = [
        ...(inner.fsrsDynamicDesiredRetentionFixedTargetDrs ?? []),
    ];
    request.fsrsDynamicDesiredRetentionMin = inner.fsrsDynamicDesiredRetentionMin;
    request.fsrsDynamicDesiredRetentionMax = inner.fsrsDynamicDesiredRetentionMax;
    request.fsrsDynamicDesiredRetentionClamp = inner.fsrsDynamicDesiredRetentionClamp;
    return request;
}
