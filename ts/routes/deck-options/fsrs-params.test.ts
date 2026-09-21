// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { fsrsSameDayEvaluationOverrideForComparison } from "./fsrs-param-diagnostics";
import { fsrsParams, fsrsParamsForEvaluation, withSelectedFsrsParams } from "./lib";

test("first FSRS-7 evaluation uses its defaults and preserves the requested comparison targets", () => {
    const config = new DeckConfig_Config({
        fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
        fsrsParams6: Array(21).fill(1),
    });
    const defaults = new DeckConfig_Config({ fsrsParams7: Array(34).fill(2) });
    const params = fsrsParamsForEvaluation(config, defaults);

    expect(params).toEqual(defaults.fsrsParams7);
    expect(config.fsrsParams7).toEqual([]);
    for (const includeSameDay of [true, false]) {
        expect(fsrsSameDayEvaluationOverrideForComparison(params, Array(34).fill(3), includeSameDay))
            .toBe(includeSameDay);
    }
});

test("evaluation keeps explicit parameters available for validation", () => {
    const defaults = new DeckConfig_Config({ fsrsParams7: Array(34).fill(2) });
    for (const params of [Array(34).fill(3), [1, 2, 3], Array(35).fill(1), Array(34).fill(NaN)]) {
        const config = new DeckConfig_Config({ fsrsParams7: params });
        expect(fsrsParamsForEvaluation(config, defaults)).toEqual(params);
    }
});

test("FSRS-6 evaluation uses the selected parameters even when FSRS-7 parameters are stored", () => {
    const defaults = new DeckConfig_Config({ fsrsParams7: Array(34).fill(2) });
    for (const params of [[], Array(21).fill(1)]) {
        const config = new DeckConfig_Config({
            fsrsVersion: DeckConfig_Config_FsrsVersion.SIX,
            fsrsParams6: params,
            fsrsParams7: Array(34).fill(3),
        });
        expect(fsrsParamsForEvaluation(config, defaults)).toEqual(params);
    }
});

test("fsrsParams prefers fsrsParams7 when valid", () => {
    const config = new DeckConfig_Config();
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => i + 1);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config)).toStrictEqual(config.fsrsParams7);
});

test("fsrsParams uses FSRS-7 defaults when stored params are not usable", () => {
    const config = new DeckConfig_Config();
    const defaults = new DeckConfig_Config();
    config.fsrsParams4 = Array.from({ length: 17 }, (_, i) => i + 1);
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 10 + i);
    config.fsrsParams7 = [0.1, 0.2, 0.3];
    defaults.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config, defaults)).toStrictEqual(defaults.fsrsParams7);
});

test("fsrsParams uses selected version when usable", () => {
    const config = new DeckConfig_Config();
    config.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 50 + i);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config)).toStrictEqual(config.fsrsParams6);
});

test("fsrsParams accepts 34-parameter FSRS-7", () => {
    const config = new DeckConfig_Config();
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => i + 1);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config)).toStrictEqual(config.fsrsParams7);
});

test("fsrsParams returns empty when no stored array is usable", () => {
    const config = new DeckConfig_Config();
    config.fsrsParams4 = [1, 2, 3];
    config.fsrsParams5 = [4, 5, 6];
    config.fsrsParams6 = [7, 8, 9];
    config.fsrsParams7 = [10, 11, 12];
    expect(fsrsParams(config)).toStrictEqual([]);
});

test("withSelectedFsrsParams updates FSRS-7 params immutably", () => {
    const config = new DeckConfig_Config();
    config.fsrsVersion = DeckConfig_Config_FsrsVersion.SEVEN;
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 10 + i);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);

    const updatedParams = Array.from({ length: 34 }, (_, i) => 200 + i);
    const updated = withSelectedFsrsParams(config, updatedParams);

    expect(updated).not.toBe(config);
    expect(updated.fsrsParams7).toStrictEqual(updatedParams);
    expect(updated.fsrsParams6).toStrictEqual(config.fsrsParams6);
    expect(config.fsrsParams7).not.toStrictEqual(updatedParams);
});

test("withSelectedFsrsParams updates selected FSRS-6 params only", () => {
    const config = new DeckConfig_Config();
    config.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 10 + i);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);

    const updatedParams = Array.from({ length: 21 }, (_, i) => 300 + i);
    const updated = withSelectedFsrsParams(config, updatedParams);

    expect(updated.fsrsParams6).toStrictEqual(updatedParams);
    expect(updated.fsrsParams7).toStrictEqual(config.fsrsParams7);
});
