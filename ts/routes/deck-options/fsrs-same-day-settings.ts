export interface Fsrs7SameDaySettings {
    includeSameDayReviewsForOptimize: boolean;
    includeSameDayReviewsForEvaluate: boolean;
}

const OPTIMIZE_KEY = "fsrs7IncludeSameDayOptimize";
const EVALUATE_KEY = "fsrs7IncludeSameDayEvaluate";

const DEFAULT_SETTINGS: Fsrs7SameDaySettings = {
    includeSameDayReviewsForOptimize: true,
    includeSameDayReviewsForEvaluate: true,
};

function readBool(value: unknown, fallback: boolean): boolean {
    return typeof value === "boolean" ? value : fallback;
}

export function readFsrs7SameDaySettings(
    auxData: Record<string, unknown>,
): Fsrs7SameDaySettings {
    return {
        includeSameDayReviewsForOptimize: readBool(
            auxData[OPTIMIZE_KEY],
            DEFAULT_SETTINGS.includeSameDayReviewsForOptimize,
        ),
        includeSameDayReviewsForEvaluate: readBool(
            auxData[EVALUATE_KEY],
            DEFAULT_SETTINGS.includeSameDayReviewsForEvaluate,
        ),
    };
}

export function withFsrs7SameDaySettings(
    auxData: Record<string, unknown>,
    settings: Fsrs7SameDaySettings,
): Record<string, unknown> | undefined {
    const current = readFsrs7SameDaySettings(auxData);
    if (
        current.includeSameDayReviewsForOptimize
            === settings.includeSameDayReviewsForOptimize
        && current.includeSameDayReviewsForEvaluate
            === settings.includeSameDayReviewsForEvaluate
    ) {
        return;
    }

    return {
        ...auxData,
        [OPTIMIZE_KEY]: settings.includeSameDayReviewsForOptimize,
        [EVALUATE_KEY]: settings.includeSameDayReviewsForEvaluate,
    };
}
