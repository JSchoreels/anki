// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
} from "./help-me-decide-defaults";

test("help me decide uses R-only blend by default", () => {
    expect(HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT).toBe(0);
});

test("help me decide leaves monotonic grade constraints disabled by default", () => {
    expect(
        HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    ).toBe(false);
});
