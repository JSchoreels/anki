// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export function metricDelta(oldValue: number, newValue: number): number {
    return newValue - oldValue;
}

export function metricDeltaPercent(oldValue: number, newValue: number): number | undefined {
    if (oldValue === 0) {
        if (newValue === 0) {
            return 0;
        }
        return undefined;
    }
    return ((newValue - oldValue) / oldValue) * 100;
}

export function deltaClass(delta: number): "better" | "worse" | "equal" {
    if (delta < 0) {
        return "better";
    }
    if (delta > 0) {
        return "worse";
    }
    return "equal";
}

export function formatMetric(value: number): string {
    return value.toFixed(4);
}

export function formatDelta(delta: number): string {
    if (delta === 0) {
        return "0.0000";
    }
    return `${delta > 0 ? "+" : ""}${delta.toFixed(4)}`;
}

export function formatPercentDelta(deltaPercent: number | undefined): string {
    if (deltaPercent === undefined) {
        return "n/a";
    }
    if (deltaPercent === 0) {
        return "0.00%";
    }
    return `${deltaPercent > 0 ? "+" : ""}${deltaPercent.toFixed(2)}%`;
}
