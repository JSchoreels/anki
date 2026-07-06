// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    readFsrs7SchedulingPenaltySettings,
    withFsrs7SchedulingPenaltySettings,
} from "./fsrs-scheduling-penalty-settings";

test("readFsrs7SchedulingPenaltySettings defaults to disabled", () => {
    expect(readFsrs7SchedulingPenaltySettings({})).toStrictEqual({
        enableSchedulingPenalties: false,
    });
});

test("readFsrs7SchedulingPenaltySettings reads stored boolean", () => {
    expect(
        readFsrs7SchedulingPenaltySettings({
            fsrs7EnableSchedulingPenalties: true,
        }),
    ).toStrictEqual({
        enableSchedulingPenalties: true,
    });
});

test("withFsrs7SchedulingPenaltySettings updates key when value differs", () => {
    expect(
        withFsrs7SchedulingPenaltySettings(
            {
                unrelated: "keep",
                fsrs7EnableSchedulingPenalties: false,
            },
            {
                enableSchedulingPenalties: true,
            },
        ),
    ).toStrictEqual({
        unrelated: "keep",
        fsrs7EnableSchedulingPenalties: true,
    });
});

test("withFsrs7SchedulingPenaltySettings is a no-op when value is unchanged", () => {
    expect(
        withFsrs7SchedulingPenaltySettings(
            {
                fsrs7EnableSchedulingPenalties: true,
            },
            {
                enableSchedulingPenalties: true,
            },
        ),
    ).toBeUndefined();
});

test("withFsrs7SchedulingPenaltySettings does not persist the default", () => {
    expect(
        withFsrs7SchedulingPenaltySettings(
            {},
            {
                enableSchedulingPenalties: false,
            },
        ),
    ).toBeUndefined();
});
