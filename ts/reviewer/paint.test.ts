// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, expect, test, vi } from "vitest";

import { waitForNextPaint } from "./paint";

afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
});

test("waits until the frame after the updated DOM can be painted", async () => {
    vi.useFakeTimers();
    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });

    let resolved = false;
    const painted = waitForNextPaint().then(() => {
        resolved = true;
    });

    frameCallbacks.shift()!(0);
    await Promise.resolve();
    expect(resolved).toBe(false);

    frameCallbacks.shift()!(16);
    await painted;
    expect(resolved).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
});

test("does not stall when animation frames are throttled", async () => {
    vi.useFakeTimers();
    vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);

    const painted = waitForNextPaint();
    await vi.advanceTimersByTimeAsync(100);

    await expect(painted).resolves.toBeUndefined();
});
