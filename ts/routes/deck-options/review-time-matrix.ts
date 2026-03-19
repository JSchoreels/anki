// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export type ReviewTimeMatrix = {
    rBucketCount: number;
    sBucketCount: number;
    failSeconds: number[];
    passSeconds: number[];
    sampleCounts: number[];
};

export function matrixCellIndex(
    rIndex: number,
    sIndex: number,
    sBucketCount: number,
): number {
    return rIndex * sBucketCount + sIndex;
}

export function matrixCellValue(
    values: number[],
    rIndex: number,
    sIndex: number,
    sBucketCount: number,
): number {
    const idx = matrixCellIndex(rIndex, sIndex, sBucketCount);
    return values[idx] ?? 0;
}

export function rBucketLabel(index: number): string {
    const upper = 100 - index * 5;
    const lower = Math.max(0, upper - 5);
    return `${lower}-${upper}%`;
}

export function sBucketBounds(
    index: number,
    sBucketCount: number,
    min = 0.001,
    max = 36500,
): [number, number] {
    const ratioLo = index / sBucketCount;
    const ratioHi = (index + 1) / sBucketCount;
    const logMin = Math.log(min);
    const logMax = Math.log(max);
    const lo = Math.exp(logMin + (logMax - logMin) * ratioLo);
    const hi = Math.exp(logMin + (logMax - logMin) * ratioHi);
    return [lo, hi];
}

export function sBucketLabel(index: number, sBucketCount: number): string {
    if (sBucketCount <= 1) {
        return "All S";
    }
    const [lo, hi] = sBucketBounds(index, sBucketCount);
    return `${lo.toPrecision(2)}-${hi.toPrecision(2)}`;
}

export function buildSLineSeries(
    values: number[],
    rBucketCount: number,
    sBucketCount: number,
): number[][] {
    const lines = Array.from({ length: sBucketCount }, () =>
        Array.from({ length: rBucketCount }, () => 0),
    );
    for (let rIndex = 0; rIndex < rBucketCount; rIndex++) {
        for (let sIndex = 0; sIndex < sBucketCount; sIndex++) {
            lines[sIndex][rIndex] = matrixCellValue(
                values,
                rIndex,
                sIndex,
                sBucketCount,
            );
        }
    }
    return lines;
}

export function buildFailPassRatioSeries(
    failValues: number[],
    passValues: number[],
    rBucketCount: number,
    sBucketCount: number,
): number[][] {
    const failLines = buildSLineSeries(failValues, rBucketCount, sBucketCount);
    const passLines = buildSLineSeries(passValues, rBucketCount, sBucketCount);
    return failLines.map((line, sIndex) =>
        line.map((failValue, rIndex) => {
            const passValue = passLines[sIndex][rIndex];
            if (passValue <= 0) {
                return 0;
            }
            return failValue / passValue;
        }),
    );
}

export function seriesMinMax(series: number[][]): [number, number] {
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    for (const line of series) {
        for (const value of line) {
            min = Math.min(min, value);
            max = Math.max(max, value);
        }
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
        return [0, 1];
    }
    if (min === max) {
        return [Math.max(0, min - 1), max + 1];
    }
    return [min, max];
}

export function median(values: number[]): number {
    if (values.length === 0) {
        return 0;
    }
    const sorted = [...values].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    if (sorted.length % 2 === 1) {
        return sorted[mid];
    }
    return (sorted[mid - 1] + sorted[mid]) / 2;
}
