// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import {
    maximumIntervalToString,
    minimumIntervalToString,
    stringToMaximumInterval,
    stringToMinimumInterval,
} from "./minimum-interval";

describe("minimum interval", () => {
    test("parses supported suffixes", () => {
        expect(stringToMinimumInterval("1s")).toBe(1);
        expect(stringToMinimumInterval("1d")).toBe(86_400);
        expect(stringToMinimumInterval("1w")).toBe(604_800);
        expect(stringToMinimumInterval("1m")).toBe(2_592_000);
        expect(stringToMinimumInterval("1y")).toBe(31_536_000);
    });

    test("formats with the largest whole supported unit", () => {
        expect(minimumIntervalToString(1)).toBe("1s");
        expect(minimumIntervalToString(86_400)).toBe("1d");
        expect(minimumIntervalToString(604_800)).toBe("1w");
        expect(minimumIntervalToString(2_592_000)).toBe("1m");
        expect(minimumIntervalToString(31_536_000)).toBe("1y");
    });

    test("caps values at the backend maximum", () => {
        expect(stringToMinimumInterval("9999m")).toBe(3_153_600_000);
    });

    test("parses maximum intervals into whole days", () => {
        expect(stringToMaximumInterval("1s")).toBe(1);
        expect(stringToMaximumInterval("1d")).toBe(1);
        expect(stringToMaximumInterval("1w")).toBe(7);
        expect(stringToMaximumInterval("1m")).toBe(30);
        expect(stringToMaximumInterval("1y")).toBe(365);
    });

    test("formats maximum intervals", () => {
        expect(maximumIntervalToString(1)).toBe("1d");
        expect(maximumIntervalToString(7)).toBe("1w");
        expect(maximumIntervalToString(30)).toBe("1m");
        expect(maximumIntervalToString(365)).toBe("1y");
    });
});
