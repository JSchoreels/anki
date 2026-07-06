// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const VALID_FSRS_PARAM_COUNTS = new Set([0, 17, 19, 21, 34]);
const OUTDATED_FSRS7_PREVIEW_PARAM_COUNT = 35;
export const OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING =
    "These FSRS-7 parameters were produced by an older preview version and have 35 values. Final FSRS-7 uses 34 values. Clear the field and optimize this preset, or run Optimize All Presets.";

export interface FsrsParamDiagnostics {
    count: number;
    validCount: boolean;
    outdatedFsrs7PreviewParams: boolean;
    nonFiniteIndexes: number[];
    nonFiniteValues: string[];
    valid: boolean;
}

export function fsrsParamDiagnostics(params: readonly number[]): FsrsParamDiagnostics {
    const nonFiniteIndexes = params.flatMap((value, index) => Number.isFinite(value) ? [] : [index]);
    const outdatedFsrs7PreviewParams = params.length === OUTDATED_FSRS7_PREVIEW_PARAM_COUNT;

    return {
        count: params.length,
        validCount: VALID_FSRS_PARAM_COUNTS.has(params.length),
        outdatedFsrs7PreviewParams,
        nonFiniteIndexes,
        nonFiniteValues: nonFiniteIndexes
            .slice(0, 5)
            .map((index) => String(params[index])),
        valid: VALID_FSRS_PARAM_COUNTS.has(params.length) && nonFiniteIndexes.length === 0,
    };
}

export function fsrsParamsSupportSameDayEvaluation(params: readonly number[]): boolean {
    return params.length === 34;
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
