// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface DisplayOrderSettings {
    intradayLearningBeforeReviews: boolean;
}

const METADATA_KEY = "jschoreels.deckoptions";
const INTRADAY_PRIORITY_KEY = "intraday_learning_before_reviews";

function metadata(auxData: Record<string, unknown>): Record<string, unknown> {
    const value = auxData[METADATA_KEY];
    return typeof value === "object" && value !== null && !Array.isArray(value)
        ? value as Record<string, unknown>
        : {};
}

export function readDisplayOrderSettings(auxData: Record<string, unknown>): DisplayOrderSettings {
    return {
        intradayLearningBeforeReviews: metadata(auxData)[INTRADAY_PRIORITY_KEY] === true,
    };
}

export function withDisplayOrderSettings(
    auxData: Record<string, unknown>,
    settings: DisplayOrderSettings,
): Record<string, unknown> | undefined {
    if (readDisplayOrderSettings(auxData).intradayLearningBeforeReviews === settings.intradayLearningBeforeReviews) {
        return;
    }
    return {
        ...auxData,
        [METADATA_KEY]: {
            ...metadata(auxData),
            [INTRADAY_PRIORITY_KEY]: settings.intradayLearningBeforeReviews,
        },
    };
}
