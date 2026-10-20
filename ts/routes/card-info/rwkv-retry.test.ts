// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { createRwkvCardInfoRetry } from "./rwkv-retry";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("retries transient RWKV failures and stops once predictions are ready", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    const retry = createRwkvCardInfoRetry(refresh);
    retry.update([{ cardId: 1n, rwkvRetryPending: true }]);
    await vi.advanceTimersByTimeAsync(500);
    expect(refresh).toHaveBeenCalledTimes(1);

    retry.update([{ cardId: 1n, rwkvRetryPending: false }]);
    await vi.runAllTimersAsync();
    expect(refresh).toHaveBeenCalledTimes(1);
});

test("caps retries while RWKV stays unavailable", async () => {
    const refresh = vi.fn(async () => {
        retry.update([{ cardId: 1n, rwkvRetryPending: true }]);
    });
    const retry = createRwkvCardInfoRetry(refresh);
    retry.update([{ cardId: 1n, rwkvRetryPending: true }]);
    await vi.runAllTimersAsync();
    expect(refresh).toHaveBeenCalledTimes(5);
});

test("cancels stale retries on card changes and when the page closes", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    const retry = createRwkvCardInfoRetry(refresh);
    retry.update([{ cardId: 1n, rwkvRetryPending: true }]);
    retry.update([{ cardId: 2n, rwkvRetryPending: false }]);
    await vi.runAllTimersAsync();
    expect(refresh).not.toHaveBeenCalled();

    retry.update([{ cardId: 2n, rwkvRetryPending: true }]);
    retry.stop();
    await vi.runAllTimersAsync();
    expect(refresh).not.toHaveBeenCalled();
});

test("retries when only the previous card is waiting for RWKV", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    const retry = createRwkvCardInfoRetry(refresh);
    retry.update([
        { cardId: 1n, rwkvRetryPending: false },
        { cardId: 2n, rwkvRetryPending: true },
    ]);
    await vi.advanceTimersByTimeAsync(500);
    expect(refresh).toHaveBeenCalledTimes(1);
});
