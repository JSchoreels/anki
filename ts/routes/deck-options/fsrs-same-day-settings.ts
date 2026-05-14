// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface Fsrs7SameDaySettings {
    includeSameDayReviews: boolean;
}

const OPTIMIZE_KEY = "fsrs7IncludeSameDayOptimize";
const EVALUATE_KEY = "fsrs7IncludeSameDayEvaluate";

const DEFAULT_SETTINGS: Fsrs7SameDaySettings = {
    includeSameDayReviews: true,
};

function readBool(value: unknown, fallback: boolean): boolean {
    return typeof value === "boolean" ? value : fallback;
}

export function readFsrs7SameDaySettings(
    auxData: Record<string, unknown>,
): Fsrs7SameDaySettings {
    return {
        includeSameDayReviews: readBool(
            auxData[OPTIMIZE_KEY],
            readBool(auxData[EVALUATE_KEY], DEFAULT_SETTINGS.includeSameDayReviews),
        ),
    };
}

export function withFsrs7SameDaySettings(
    auxData: Record<string, unknown>,
    settings: Fsrs7SameDaySettings,
): Record<string, unknown> | undefined {
    const current = readFsrs7SameDaySettings(auxData);
    const hasLegacyEvaluateKey = EVALUATE_KEY in auxData;
    if (
        current.includeSameDayReviews === settings.includeSameDayReviews
        && !hasLegacyEvaluateKey
    ) {
        return;
    }

    const { [EVALUATE_KEY]: _, ...rest } = auxData;
    return {
        ...rest,
        [OPTIMIZE_KEY]: settings.includeSameDayReviews,
    };
}
