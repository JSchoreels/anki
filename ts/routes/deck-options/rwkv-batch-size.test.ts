// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    RWKV_REVIEW_BATCH_SIZE_RECOMMENDED,
    rwkvBatchSizeForSliderIndex,
    rwkvBatchSizeOption,
    rwkvBatchSizeSliderIndex,
    rwkvEstimatedMemoryLabel,
} from "./rwkv-batch-size";

test("RWKV batch size defaults to the recommended option", () => {
    expect(rwkvBatchSizeOption(0).size).toBe(RWKV_REVIEW_BATCH_SIZE_RECOMMENDED);
    expect(rwkvBatchSizeOption(Number.NaN).size).toBe(
        RWKV_REVIEW_BATCH_SIZE_RECOMMENDED,
    );
});

test("RWKV batch slider maps to selectable batch sizes", () => {
    expect(rwkvBatchSizeForSliderIndex(-1)).toBe(64);
    expect(rwkvBatchSizeForSliderIndex(3)).toBe(512);
    expect(rwkvBatchSizeForSliderIndex(99)).toBe(2048);
});

test("RWKV batch slider chooses the nearest option for existing values", () => {
    expect(rwkvBatchSizeSliderIndex(300)).toBe(2);
    expect(rwkvBatchSizeOption(900).size).toBe(1024);
});

test("RWKV batch memory label uses benchmark estimates", () => {
    expect(rwkvEstimatedMemoryLabel(512)).toBe("136 MB");
    expect(rwkvEstimatedMemoryLabel(2048)).toBe("545 MB");
});
