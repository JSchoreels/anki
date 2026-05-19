// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

import { prepareData, stabilityS90 } from "./forgetting-curve";

function fsrs7Params(): number[] {
    const params = Array(35).fill(0);
    params[27] = 1;
    params[28] = 1;
    params[29] = 0.8;
    params[30] = 0.8;
    params[31] = 1;
    return params;
}

test("stabilityS90 returns raw stability for scalar FSRS curves", () => {
    expect(stabilityS90(10, 0.1542, undefined)).toBe(10);
});

test("stabilityS90 derives S90 from FSRS-7 curve params", () => {
    expect(stabilityS90(10, 0.1542, fsrs7Params())).toBeCloseTo(40 / 9, 3);
});

test("prepareData carries S90 for the forgetting curve tooltip", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-03T00:00:00Z"));

    try {
        const data = prepareData(
            [
                {
                    time: Date.parse("2024-01-01T00:00:00Z") / 1000,
                    memoryState: { stability: 10, difficulty: 5 },
                },
            ] as any,
            2,
            0.1542,
            fsrs7Params(),
        );

        expect(data.at(-1)?.stability).toBe(10);
        expect(data.at(-1)?.stabilityS90).toBeCloseTo(40 / 9, 3);
    } finally {
        vi.useRealTimers();
    }
});
