// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { customDecayCandidates, formatDecay, supportsCustomDecayTable, withLastParam } from "./custom-decay-table";

test("custom decay candidates are the requested values", () => {
    expect(customDecayCandidates).toStrictEqual([0.1, 0.15, 0.2, 0.25, 0.35, 0.4]);
});

test("withLastParam only updates final parameter", () => {
    expect(withLastParam([1, 2, 3], 0.25)).toStrictEqual([1, 2, 0.25]);
    expect(withLastParam([], 0.25)).toStrictEqual([]);
});

test("custom decay table is disabled for FSRS-7 parameter sets", () => {
    expect(supportsCustomDecayTable(Array.from({ length: 21 }, (_, i) => i))).toBe(true);
    expect(supportsCustomDecayTable(Array.from({ length: 34 }, (_, i) => i))).toBe(false);
});

test("formatDecay keeps two decimals", () => {
    expect(formatDecay(0.1)).toBe("0.10");
    expect(formatDecay(0.25)).toBe("0.25");
});
