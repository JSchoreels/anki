// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
    bridgeCommand: vi.fn(),
    preloadResources: vi.fn<(html: string) => Promise<void>>(),
}));

vi.mock("jquery/dist/jquery", () => ({ default: {} }));
vi.mock("@tslib/bridgecommand", () => ({ bridgeCommand: mocks.bridgeCommand }));
vi.mock("@tslib/runtime-require", () => ({ registerPackage: vi.fn() }));
vi.mock("../routes/image-occlusion/review", () => ({
    imageOcclusionAPI: { setup: vi.fn() },
}));
vi.mock("./answering", () => ({ mutateNextCardStates: vi.fn() }));
vi.mock("./browser_selector", () => ({ addBrowserClasses: vi.fn() }));
vi.mock("./images", () => ({
    allImagesLoaded: () => Promise.resolve([]),
    preloadAnswerImages: vi.fn(),
}));
vi.mock("./preload", () => ({ preloadResources: mocks.preloadResources }));

import { _showQuestion } from "./index";
import { waitForNextPaint } from "./paint";

interface Deferred {
    promise: Promise<void>;
    resolve: () => void;
}

function deferred(): Deferred {
    let resolve!: () => void;
    const promise = new Promise<void>((resolvePromise) => {
        resolve = resolvePromise;
    });
    return { promise, resolve };
}

async function flushPromises(): Promise<void> {
    for (let i = 0; i < 20; i++) {
        await Promise.resolve();
    }
}

beforeEach(() => {
    document.body.innerHTML = "<div id=\"qa\">old answer</div>";
    mocks.bridgeCommand.mockReset();
    mocks.preloadResources.mockReset();
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    Object.assign(globalThis, {
        MathJax: {
            startup: { promise: Promise.resolve() },
            typesetClear: vi.fn(),
            typesetPromise: () => Promise.resolve(),
        },
    });
});

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
    const painted = waitForNextPaint(vi.fn(), vi.fn()).then(() => {
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

test("reports a stall without treating it as a paint", async () => {
    vi.useFakeTimers();
    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });
    const onStalled = vi.fn();
    const onRetry = vi.fn();
    let resolved = false;

    const painted = waitForNextPaint(onStalled, onRetry).then(() => {
        resolved = true;
    });
    await vi.advanceTimersByTimeAsync(100);

    expect(onStalled).toHaveBeenCalledOnce();
    expect(onRetry).not.toHaveBeenCalled();
    expect(resolved).toBe(false);

    await vi.advanceTimersByTimeAsync(900);

    expect(onRetry).toHaveBeenCalledTimes(2);
    expect(resolved).toBe(false);

    frameCallbacks.shift()!(100);
    frameCallbacks.shift()!(116);
    await painted;
    expect(resolved).toBe(true);
});

test("keeps the old card while preloading and only commits the latest update", async () => {
    const firstPreload = deferred();
    const secondPreload = deferred();
    mocks.preloadResources.mockImplementation((html) => {
        if (html === "first question") {
            return firstPreload.promise;
        }
        expect(html).toBe("second question");
        return secondPreload.promise;
    });

    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });

    _showQuestion(
        "first question",
        "first answer",
        "",
        "question:1:101",
    );
    await flushPromises();

    const qa = document.getElementById("qa")!;
    expect(qa.innerHTML).toBe("old answer");
    expect(qa.style.opacity).toBe("");

    _showQuestion(
        "second question",
        "second answer",
        "",
        "question:2:202",
    );
    firstPreload.resolve();
    await flushPromises();

    expect(qa.innerHTML).toBe("old answer");
    expect(mocks.preloadResources).toHaveBeenCalledTimes(2);

    secondPreload.resolve();
    await flushPromises();

    expect(qa.innerHTML).toBe("second question");
    expect(qa.style.opacity).toBe("");
    expect(frameCallbacks).toHaveLength(1);
    frameCallbacks.shift()!(0);
    frameCallbacks.shift()!(16);
    await flushPromises();

    expect(mocks.bridgeCommand).toHaveBeenCalledWith(
        "qaPresented:question:2:202",
    );
    expect(mocks.bridgeCommand).not.toHaveBeenCalledWith(
        "qaPresented:question:1:101",
    );
});

test("a render superseded while painting never presents as current", async () => {
    mocks.preloadResources.mockResolvedValue();
    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });

    _showQuestion("first question", "first answer", "", "question:3:303");
    await flushPromises();

    expect(document.getElementById("qa")!.innerHTML).toBe("first question");
    expect(frameCallbacks).toHaveLength(1);

    _showQuestion("second question", "second answer", "", "question:4:404");
    await flushPromises();

    expect(document.getElementById("qa")!.innerHTML).toBe("first question");

    frameCallbacks.shift()!(0);
    frameCallbacks.shift()!(16);
    await flushPromises();

    expect(document.getElementById("qa")!.innerHTML).toBe("second question");
    expect(mocks.bridgeCommand).not.toHaveBeenCalledWith(
        "qaPresented:question:3:303",
    );
    expect(frameCallbacks).toHaveLength(1);

    frameCallbacks.shift()!(32);
    frameCallbacks.shift()!(48);
    await flushPromises();
    expect(mocks.bridgeCommand).toHaveBeenCalledWith(
        "qaPresented:question:4:404",
    );
});
