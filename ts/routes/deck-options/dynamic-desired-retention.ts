// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export const COST_ADR_PARAMETER_COUNT = 15;
export const COST_ADR_WEIGHT_MIN = 0;
export const COST_ADR_WEIGHT_MAX = 1024;
export const COST_ADR_RETENTION_MIN = 0.3;
export const COST_ADR_RETENTION_MAX = 0.995;
const S_MIN = 0.0001;
const S_MAX = 36500;
const D_MIN = 1;
const D_MAX = 10;

export function dynamicDesiredRetentionEnabled(config: {
    fsrsDynamicDesiredRetentionEnabled: boolean;
    fsrsDynamicDesiredRetentionParams: number[];
    fsrsDynamicDesiredRetentionWeights: number[];
    fsrsDynamicDesiredRetentionAvgDrs: number[];
}): boolean {
    return config.fsrsDynamicDesiredRetentionEnabled
        && validPolicyParams(config.fsrsDynamicDesiredRetentionParams)
        && validCalibration(
            config.fsrsDynamicDesiredRetentionWeights,
            config.fsrsDynamicDesiredRetentionAvgDrs,
        );
}

export function validPolicyParams(params: number[]): boolean {
    return params.length === COST_ADR_PARAMETER_COUNT
        && params.every((value) => Number.isFinite(value));
}

export function validCalibration(weights: number[], avgDrs: number[]): boolean {
    return weights.length === avgDrs.length
        && weights.length >= 2
        && weights.every((value) => Number.isFinite(value) && value >= 0)
        && avgDrs.every((value) => Number.isFinite(value) && value >= 0 && value <= 1);
}

export function costWeightForAverageDr(
    target: number,
    weights: number[],
    avgDrs: number[],
): number | null {
    if (!Number.isFinite(target) || !validCalibration(weights, avgDrs)) {
        return null;
    }
    const calibration = weights
        .map((weight, index) => ({ weight, avgDr: avgDrs[index] }))
        .sort((a, b) => a.weight - b.weight);
    for (let i = 0; i < calibration.length - 1; i++) {
        const left = calibration[i];
        const right = calibration[i + 1];
        if ((left.avgDr - target) * (right.avgDr - target) > 0) {
            continue;
        }
        if (Math.abs(left.avgDr - right.avgDr) < Number.EPSILON) {
            return left.weight;
        }
        const t = clamp((target - left.avgDr) / (right.avgDr - left.avgDr), 0, 1);
        const leftLog = Math.log1p(left.weight);
        const rightLog = Math.log1p(right.weight);
        return Math.expm1(leftLog + (rightLog - leftLog) * t);
    }
    return null;
}

export function evaluateDynamicDesiredRetention(
    params: number[],
    stability: number,
    difficulty: number,
    costWeight: number,
): number {
    const phi = stateFeatures(stability, difficulty);
    const z = normalizedCostWeight(costWeight);
    const base = dot(params.slice(0, 5), phi);
    const zEffect = softplus(dot(params.slice(5, 10), phi)) * z;
    const z2Effect = softplus(dot(params.slice(10, 15), phi)) * z * z;
    return COST_ADR_RETENTION_MIN
        + (COST_ADR_RETENTION_MAX - COST_ADR_RETENTION_MIN)
            * sigmoid(base - zEffect - z2Effect);
}

function stateFeatures(stability: number, difficulty: number): number[] {
    const s = clamp(stability, S_MIN, S_MAX);
    const d = clamp(difficulty, D_MIN, D_MAX);
    const logSMin = Math.log(S_MIN);
    const logSSpan = Math.log(S_MAX) - logSMin;
    const xS = clamp((Math.log(s) - logSMin) / logSSpan, 0, 1);
    const xD = clamp((d - D_MIN) / (D_MAX - D_MIN), 0, 1);
    return [1, xS, xD, xS * xD, xS * xS];
}

function normalizedCostWeight(costWeight: number): number {
    const weight = clamp(costWeight, COST_ADR_WEIGHT_MIN, COST_ADR_WEIGHT_MAX);
    const lo = Math.log1p(COST_ADR_WEIGHT_MIN);
    const hi = Math.log1p(COST_ADR_WEIGHT_MAX);
    return clamp((Math.log1p(weight) - lo) / (hi - lo), 0, 1);
}

function dot(lhs: number[], rhs: number[]): number {
    return lhs.reduce((total, value, index) => total + value * rhs[index], 0);
}

function sigmoid(value: number): number {
    if (value >= 0) {
        const z = Math.exp(-value);
        return 1 / (1 + z);
    }
    const z = Math.exp(value);
    return z / (1 + z);
}

function softplus(value: number): number {
    if (value > 20) {
        return value;
    } else if (value < -20) {
        return Math.exp(value);
    }
    return Math.log1p(Math.exp(value));
}

function clamp(value: number, min: number, max: number): number {
    return Math.min(max, Math.max(min, value));
}
