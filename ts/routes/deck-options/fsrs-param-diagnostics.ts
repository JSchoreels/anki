// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const VALID_FSRS_PARAM_COUNTS = new Set([0, 17, 19, 21, 35]);

export interface FsrsParamDiagnostics {
    count: number;
    validCount: boolean;
    nonFiniteIndexes: number[];
    nonFiniteValues: string[];
    valid: boolean;
}

export function fsrsParamDiagnostics(
    params: readonly number[],
): FsrsParamDiagnostics {
    const nonFiniteIndexes = params.flatMap((value, index) => Number.isFinite(value) ? [] : [index]);

    return {
        count: params.length,
        validCount: VALID_FSRS_PARAM_COUNTS.has(params.length),
        nonFiniteIndexes,
        nonFiniteValues: nonFiniteIndexes
            .slice(0, 5)
            .map((index) => String(params[index])),
        valid: VALID_FSRS_PARAM_COUNTS.has(params.length)
            && nonFiniteIndexes.length === 0,
    };
}

export function fsrsParamsSupportSameDayEvaluation(
    params: readonly number[],
): boolean {
    return params.length === 35;
}

export function fsrsSameDayEvaluationOverrideForComparison(
    currentParams: readonly number[],
    optimizedParams: readonly number[],
    requestedOverride: boolean | undefined,
): boolean | undefined {
    if (requestedOverride !== true) {
        return requestedOverride;
    }

    return fsrsParamsSupportSameDayEvaluation(currentParams)
            && fsrsParamsSupportSameDayEvaluation(optimizedParams)
        ? true
        : false;
}
