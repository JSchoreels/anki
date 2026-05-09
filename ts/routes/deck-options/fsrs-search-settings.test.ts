// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { readFsrsSearchSettings, withFsrsSearchSettings } from "./fsrs-search-settings";

test("readFsrsSearchSettings defaults to empty evaluation search", () => {
    expect(readFsrsSearchSettings({})).toStrictEqual({
        evaluationSearch: "",
    });
});

test("readFsrsSearchSettings reads persisted evaluation search", () => {
    expect(
        readFsrsSearchSettings({
            fsrsEvaluationSearch: "deck:Japanese is:review",
        }),
    ).toStrictEqual({
        evaluationSearch: "deck:Japanese is:review",
    });
});

test("withFsrsSearchSettings updates key when changed", () => {
    expect(
        withFsrsSearchSettings(
            { untouched: 1, fsrsEvaluationSearch: "" },
            { evaluationSearch: "deck:French" },
        ),
    ).toStrictEqual({
        untouched: 1,
        fsrsEvaluationSearch: "deck:French",
    });
});

test("withFsrsSearchSettings is no-op when unchanged", () => {
    expect(
        withFsrsSearchSettings(
            { fsrsEvaluationSearch: "deck:French" },
            { evaluationSearch: "deck:French" },
        ),
    ).toBeUndefined();
});
