// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";
import {
    DeckConfig_Config_FsrsVersion,
    type DeckConfig,
    type DeckConfig_Config,
} from "@generated/anki/deck_config_pb";

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
    request.historicalRetention = inner.historicalRetention;
    request.learningStepCount = inner.learnSteps.length;
    request.relearningStepCount = inner.relearnSteps.length;
    request.reviewFuzzBase = inner.reviewFuzzEnabled ? inner.reviewFuzzBase : 0;
    request.reviewFuzzFactorShort = inner.reviewFuzzEnabled
        ? inner.reviewFuzzFactorShort
        : 0;
    request.reviewFuzzFactorMid = inner.reviewFuzzEnabled
        ? inner.reviewFuzzFactorMid
        : 0;
    request.reviewFuzzFactorLong = inner.reviewFuzzEnabled
        ? inner.reviewFuzzFactorLong
        : 0;
    return request;
}
