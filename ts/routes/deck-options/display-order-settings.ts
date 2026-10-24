// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config_ReviewMix as ReviewMix } from "@generated/anki/deck_config_pb";

export interface DisplayOrderSettings {
    intradayLearningMix: ReviewMix;
}

const METADATA_KEY = "jschoreels.deckoptions";
const INTRADAY_MIX_KEY = "intraday_learning_mix";
const LEGACY_PRIORITY_KEY = "intraday_learning_before_reviews";

function metadata(auxData: Record<string, unknown>): Record<string, unknown> {
    const value = auxData[METADATA_KEY];
    return typeof value === "object" && value !== null && !Array.isArray(value)
        ? value as Record<string, unknown>
        : {};
}

export function readDisplayOrderSettings(auxData: Record<string, unknown>): DisplayOrderSettings {
    const data = metadata(auxData);
    const stored = data[INTRADAY_MIX_KEY];
    if (
        stored === ReviewMix.BEFORE_REVIEWS || stored === ReviewMix.AFTER_REVIEWS
        || stored === ReviewMix.MIX_WITH_REVIEWS
    ) {
        return { intradayLearningMix: stored };
    }
    return {
        intradayLearningMix: !(INTRADAY_MIX_KEY in data) && data[LEGACY_PRIORITY_KEY] === true
            ? ReviewMix.BEFORE_REVIEWS
            : ReviewMix.MIX_WITH_REVIEWS,
    };
}

export function withDisplayOrderSettings(
    auxData: Record<string, unknown>,
    settings: DisplayOrderSettings,
): Record<string, unknown> | undefined {
    const data = metadata(auxData);
    if (
        readDisplayOrderSettings(auxData).intradayLearningMix === settings.intradayLearningMix
        && !(LEGACY_PRIORITY_KEY in data)
    ) {
        return;
    }
    const { [LEGACY_PRIORITY_KEY]: _, ...rest } = data;
    return {
        ...auxData,
        [METADATA_KEY]: {
            ...rest,
            [INTRADAY_MIX_KEY]: settings.intradayLearningMix,
        },
    };
}
