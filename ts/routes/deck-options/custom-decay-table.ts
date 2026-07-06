// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export const customDecayCandidates = [0.1, 0.15, 0.2, 0.25, 0.35, 0.4];

export function supportsCustomDecayTable(params: number[]): boolean {
    return params.length > 0 && params.length < 34;
}

export function withLastParam(params: number[], value: number): number[] {
    if (params.length === 0) {
        return [];
    }
    const next = [...params];
    next[next.length - 1] = value;
    return next;
}

export function formatDecay(decay: number): string {
    return decay.toFixed(2);
}
