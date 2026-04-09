// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { CardStatsResponse } from "@generated/anki/stats_pb";
import { expect, test, vi } from "vitest";

import { stabilityS90ForCardInfo } from "./fsrs-stability";

function cardInfoWithMemoryState(): CardStatsResponse {
    return {
        cardId: 123n,
        memoryState: { stability: 42.0, difficulty: 5.0 },
    } as CardStatsResponse;
}

test("returns null when card info is missing", async () => {
    const fsrsNextInterval = vi.fn();
    await expect(stabilityS90ForCardInfo(null, fsrsNextInterval as any)).resolves.toBeNull();
    expect(fsrsNextInterval).not.toHaveBeenCalled();
});

test("returns null when memory state is missing", async () => {
    const fsrsNextInterval = vi.fn();
    const info = { cardId: 123n } as CardStatsResponse;
    await expect(stabilityS90ForCardInfo(info, fsrsNextInterval as any)).resolves.toBeNull();
    expect(fsrsNextInterval).not.toHaveBeenCalled();
});

test("uses scheduler helper to compute S90", async () => {
    const fsrsNextInterval = vi.fn().mockResolvedValue({ interval: 37.5 });
    const info = cardInfoWithMemoryState();
    await expect(stabilityS90ForCardInfo(info, fsrsNextInterval as any)).resolves.toBe(37.5);
    expect(fsrsNextInterval).toHaveBeenCalledWith({
        cardId: 123n,
        stability: 42.0,
        desiredRetention: 0.9,
    });
});
