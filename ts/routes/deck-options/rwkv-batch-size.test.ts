// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { rwkvEstimatedMemoryLabel } from "./rwkv-batch-size";

test("RWKV batch memory label estimates the entered batch size", () => {
    expect(rwkvEstimatedMemoryLabel(64)).toBe("17 MB");
    expect(rwkvEstimatedMemoryLabel(512)).toBe("136 MB");
    expect(rwkvEstimatedMemoryLabel(768)).toBe("204 MB");
    expect(rwkvEstimatedMemoryLabel(2048)).toBe("545 MB");
    expect(rwkvEstimatedMemoryLabel(8192)).toBe("2179 MB");
});

test("RWKV batch memory estimate clamps invalid values", () => {
    expect(rwkvEstimatedMemoryLabel(0)).toBe("17 MB");
    expect(rwkvEstimatedMemoryLabel(32768)).toBe("2179 MB");
    expect(rwkvEstimatedMemoryLabel(Number.NaN)).toBe("136 MB");
});
