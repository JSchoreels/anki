// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/* eslint
@typescript-eslint/no-explicit-any: "off",
 */

import { protoBase64 } from "@bufbuild/protobuf";
import {
    DeckConfig_Config_FsrsVersion,
    DeckConfig_Config_LeechAction,
    DeckConfigsForUpdate,
    DeckConfigsForUpdate_CurrentDeck_Limits,
    RwkvProfileSettings,
    UpdateDeckConfigsMode,
} from "@generated/anki/deck_config_pb";
import { get } from "svelte/store";
import { expect, test } from "vitest";

import { defaultScheduleCopyOptions } from "./apply-schedule";
import { DeckOptionsState } from "./lib";

const exampleData = {
    allConfig: [
        {
            config: {
                id: 1n,
                name: "Default",
                mtimeSecs: 1618570764n,
                usn: -1,
                config: {
                    learnSteps: [1, 10],
                    relearnSteps: [10],
                    newPerDay: 10,
                    reviewsPerDay: 200,
                    initialEase: 2.5,
                    easyMultiplier: 1.2999999523162842,
                    hardMultiplier: 1.2000000476837158,
                    intervalMultiplier: 1,
                    maximumReviewInterval: 36500,
                    minimumLapseInterval: 1,
                    graduatingIntervalGood: 1,
                    graduatingIntervalEasy: 4,
                    leechAction: DeckConfig_Config_LeechAction.TAG_ONLY,
                    leechThreshold: 8,
                    capAnswerTimeToSecs: 60,
                    other: protoBase64.dec(
                        "eyJuZXciOnsic2VwYXJhdGUiOnRydWV9LCJyZXYiOnsiZnV6eiI6MC4wNSwibWluU3BhY2UiOjF9fQ==",
                    ),
                },
            },
            useCount: 1,
        },
        {
            config: {
                id: 1618570764780n,
                name: "another one",
                mtimeSecs: 1618570781n,
                usn: -1,
                config: {
                    learnSteps: [1, 10, 20, 30],
                    relearnSteps: [10],
                    newPerDay: 40,
                    reviewsPerDay: 200,
                    initialEase: 2.5,
                    easyMultiplier: 1.2999999523162842,
                    hardMultiplier: 1.2000000476837158,
                    intervalMultiplier: 1,
                    maximumReviewInterval: 36500,
                    minimumLapseInterval: 1,
                    graduatingIntervalGood: 1,
                    graduatingIntervalEasy: 4,
                    leechAction: DeckConfig_Config_LeechAction.TAG_ONLY,
                    leechThreshold: 8,
                    capAnswerTimeToSecs: 60,
                },
            },
            useCount: 1,
        },
    ],
    currentDeck: {
        name: "Default::\"child\"",
        configId: 1618570764780n,
        subtreeConfigIds: [1n, 1618570764780n],
    },
    defaults: {
        config: {
            learnSteps: [1, 10],
            relearnSteps: [10],
            newPerDay: 20,
            reviewsPerDay: 200,
            initialEase: 2.5,
            easyMultiplier: 1.2999999523162842,
            hardMultiplier: 1.2000000476837158,
            intervalMultiplier: 1,
            maximumReviewInterval: 36500,
            minimumLapseInterval: 1,
            graduatingIntervalGood: 1,
            graduatingIntervalEasy: 4,
            leechAction: DeckConfig_Config_LeechAction.TAG_ONLY,
            leechThreshold: 8,
            capAnswerTimeToSecs: 60,
        },
    },
};

function startingState(): DeckOptionsState {
    return new DeckOptionsState(123n, new DeckConfigsForUpdate(exampleData));
}

test.each([
    ["legacy", "legacy"],
    ["sm40", "sm72e25"],
    ["sm36", "sm72e25"],
    ["sm55", "sm72e25"],
    ["sm56", "sm72e25"],
    ["sm72", "sm72e25"],
    ["sm72e20", "sm72e25"],
    ["sm72e25", "sm72e25"],
])("saving RWKV model %s selects %s without changing precision or pending state", (model, expected) => {
    const data = new DeckConfigsForUpdate(exampleData);
    data.rwkvProfileSettings = new RwkvProfileSettings({ model, statePrecision: "int8" });
    const state = new DeckOptionsState(123n, data);
    expect(state.dataForSaving(UpdateDeckConfigsMode.NORMAL).rwkvProfileSettings).toMatchObject({
        model: expected,
        statePrecision: "int8",
    });
    expect(state.dataForSaving(UpdateDeckConfigsMode.COMPUTE_ALL_PARAMS).rwkvProfileSettings?.model).toBe(model);
    expect(get(state.rwkvProfileSettings).model).toBe(model);
    expect(data.rwkvProfileSettings.model).toBe(model);
});

test("RWKV model and storage are global pending changes, independent of the selected preset", async () => {
    const state = startingState();
    state.resolveOriginalConfigs();
    expect(await state.isModified()).toBe(false);
    state.rwkvProfileSettings.update((settings) => {
        settings.model = "legacy";
        settings.statePrecision = "int8";
        return settings;
    });
    expect(await state.isModified()).toBe(true);
    state.setCurrentIndex(1);
    expect(get(state.rwkvProfileSettings).model).toBe("legacy");
    expect(state.dataForSaving(UpdateDeckConfigsMode.NORMAL).rwkvProfileSettings).toMatchObject({
        model: "legacy",
        statePrecision: "int8",
    });
    expect(state.dataForSaving(UpdateDeckConfigsMode.NORMAL).configs.at(-1)?.config).not.toHaveProperty(
        "rwkvProfileSettings",
    );
});

test("start", () => {
    const state = startingState();
    expect(state.currentDeck.name).toBe("Default::\"child\"");
});

test("subtree presets and search names", () => {
    const state = startingState();
    expect(state.getSubtreeConfigIds()).toStrictEqual([1n, 1618570764780n]);
    expect(state.getConfigById(1n)?.name).toBe("Default");
    expect(state.getCurrentNameForSearch()).toBe("another one");
    expect(state.getCurrentDeckNameForSearch()).toBe("Default::\\\"child\\\"");
});

test("deck list", () => {
    const state = startingState();
    expect(get(state.configList)).toStrictEqual([
        {
            current: true,
            idx: 0,
            name: "another one",
            useCount: 1,
        },
        {
            current: false,
            idx: 1,
            name: "Default",
            useCount: 1,
        },
    ]);
    expect(get(state.currentConfig).newPerDay).toBe(40);

    // rename
    state.setCurrentName("zzz");
    expect(get(state.configList)).toStrictEqual([
        {
            current: false,
            idx: 0,
            name: "Default",
            useCount: 1,
        },
        {
            current: true,
            idx: 1,
            name: "zzz",
            useCount: 1,
        },
    ]);

    // add
    state.addConfig("hello");
    expect(get(state.configList)).toStrictEqual([
        {
            current: false,
            idx: 0,
            name: "Default",
            useCount: 1,
        },
        {
            current: true,
            idx: 1,
            name: "hello",
            useCount: 1,
        },
        {
            current: false,
            idx: 2,
            name: "zzz",
            useCount: 0,
        },
    ]);
    expect(get(state.currentConfig).newPerDay).toBe(20);

    // change current
    state.setCurrentIndex(0);
    expect(get(state.configList)).toStrictEqual([
        {
            current: true,
            idx: 0,
            name: "Default",
            useCount: 2,
        },
        {
            current: false,
            idx: 1,
            name: "hello",
            useCount: 0,
        },
        {
            current: false,
            idx: 2,
            name: "zzz",
            useCount: 0,
        },
    ]);
    expect(get(state.currentConfig).newPerDay).toBe(10);

    // can't delete default
    expect(() => state.removeCurrentConfig()).toThrow();

    // deleting old deck should work
    state.setCurrentIndex(1);
    state.removeCurrentConfig();
    expect(get(state.currentConfig).newPerDay).toBe(10);

    // as should newly added one
    state.setCurrentIndex(1);
    state.removeCurrentConfig();
    expect(get(state.currentConfig).newPerDay).toBe(10);

    // only the pre-existing deck should be listed for removal
    const out = state.dataForSaving(UpdateDeckConfigsMode.NORMAL);
    expect(out.removedConfigIds).toStrictEqual([1618570764780n]);
});

test("duplicate name", () => {
    const state = startingState();

    // duplicate will get renamed
    state.addConfig("another one");
    expect(get(state.configList).find((e) => e.current)?.name).toMatch(/another.*\d+$/);

    // should handle renames too
    state.setCurrentName("Default");
    expect(get(state.configList).find((e) => e.current)?.name).toMatch(/Default\d+$/);
});

test("saving", () => {
    let state = startingState();
    let out = state.dataForSaving(UpdateDeckConfigsMode.NORMAL);
    expect(out.removedConfigIds).toStrictEqual([]);
    expect(out.targetDeckId).toBe(123n);
    // in no-changes case, currently selected config should
    // be returned
    expect(out.configs!.length).toBe(1);
    expect(out.configs![0].name).toBe("another one");
    expect(out.mode).toBe(UpdateDeckConfigsMode.NORMAL);
    expect(out.fsrsShortTermWithStepsEnabled).toBe(false);
    expect(out.fsrsLearningQueuesDisabled).toBe(false);

    state.fsrsShortTermWithStepsEnabled.set(true);
    state.fsrsLearningQueuesDisabled.set(true);
    out = state.dataForSaving(UpdateDeckConfigsMode.NORMAL);
    expect(out.fsrsShortTermWithStepsEnabled).toBe(true);
    expect(out.fsrsLearningQueuesDisabled).toBe(true);

    // rename, then change current deck
    state.setCurrentName("zzz");
    out = state.dataForSaving(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);
    state.setCurrentIndex(0);

    // renamed deck should be in changes, with current deck as last element
    out = state.dataForSaving(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);
    expect(out.configs!.map((c) => c.name)).toStrictEqual(["zzz", "Default"]);
    expect(out.mode).toBe(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);

    // start again, adding new deck
    state = startingState();
    state.addConfig("hello");

    // deleting it should not change removedConfigs
    state.removeCurrentConfig();
    out = state.dataForSaving(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);
    expect(out.removedConfigIds).toStrictEqual([]);

    // select the other non-default deck & remove
    state.setCurrentIndex(0);
    state.removeCurrentConfig();

    // should be listed in removedConfigs, and modified should
    // only contain Default, which is the new current deck
    out = state.dataForSaving(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);
    expect(out.removedConfigIds).toStrictEqual([1618570764780n]);
    expect(out.configs!.map((c) => c.name)).toStrictEqual(["Default"]);
});

test("clears incompatible FSRS params across presets for optimize all", () => {
    const state = startingState();
    const defaultConfig = state.getConfigById(1n)!;
    const otherConfig = state.getConfigById(1618570764780n)!;

    defaultConfig.config!.fsrsVersion = DeckConfig_Config_FsrsVersion.SEVEN;
    defaultConfig.config!.fsrsParams7 = Array(35).fill(1);
    otherConfig.config!.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    otherConfig.config!.fsrsParams6 = [Number.NaN, ...Array(20).fill(1)];

    expect(state.incompatibleFsrsParamPresetNames()).toStrictEqual([
        "another one",
        "Default",
    ]);
    expect(state.clearIncompatibleFsrsParams()).toBe(2);
    expect(state.incompatibleFsrsParamPresetNames()).toStrictEqual([]);
    expect(get(state.currentConfig).fsrsParams6).toStrictEqual([]);

    const out = state.dataForSaving(UpdateDeckConfigsMode.COMPUTE_ALL_PARAMS);
    expect(out.configs!.map((config) => config.name)).toStrictEqual([
        "Default",
        "another one",
    ]);
    expect(out.configs![0].config!.fsrsParams7).toStrictEqual([]);
    expect(out.configs![1].config!.fsrsParams6).toStrictEqual([]);
});

test("aux data", () => {
    const state = startingState();
    expect(get(state.currentAuxData)).toStrictEqual({});
    state.currentAuxData.update((val) => {
        return { ...val, hello: "world" };
    });

    // check default
    state.setCurrentIndex(1);
    expect(get(state.currentAuxData)).toStrictEqual({
        new: {
            separate: true,
        },
        rev: {
            fuzz: 0.05,
            minSpace: 1,
        },
    });
    state.currentAuxData.update((val) => {
        return { ...val, defaultAddition: true };
    });

    // ensure changes serialize
    const out = state.dataForSaving(UpdateDeckConfigsMode.APPLY_TO_CHILDREN);
    expect(out.configs!.length).toBe(2);
    const json = out.configs!.map((c) => JSON.parse(new TextDecoder().decode(c.config!.other)));
    expect(json).toStrictEqual([
        // other deck comes first
        {
            hello: "world",
        },
        // default is selected, so comes last
        {
            defaultAddition: true,
            new: {
                separate: true,
            },
            rev: {
                fuzz: 0.05,
                minSpace: 1,
            },
        },
    ]);
});

test("bulk schedule copies save changed presets while preserving deck limits and unrelated fields", async () => {
    const data = new DeckConfigsForUpdate(exampleData);
    data.currentDeck!.limits = new DeckConfigsForUpdate_CurrentDeck_Limits({ desiredRetention: 0.95 });
    const source = data.allConfig[1].config!.config!;
    source.rwkvReviewInstantOrderEnabled = true;
    source.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    source.desiredRetention = 0.9;
    source.fsrsParams6 = Array(21).fill(2);
    source.rwkvReviewMinElapsedSecs = 30;
    source.fsrsDynamicDesiredRetentionParams = Array(15).fill(3);
    source.other = new TextEncoder().encode(JSON.stringify({ fsrs7IncludeSameDayOptimize: false, addon: "source" }));
    const state = new DeckOptionsState(123n, data);
    state.resolveOriginalConfigs();
    state.addConfig("new preset");
    state.setCurrentIndex(get(state.configList).findIndex((entry) => entry.name === "another one"));
    state.applyScheduleToAllPresets(defaultScheduleCopyOptions());

    const out = state.dataForSaving(UpdateDeckConfigsMode.NORMAL);
    expect(out.configs!.map((config) => config.name)).toEqual(["Default", "new preset", "another one"]);
    for (const config of out.configs!) {
        expect(config.config!.rwkvReviewInstantOrderEnabled).toBe(true);
        expect(config.config!.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SIX);
        expect(config.config!.desiredRetention).toBe(0.9);
        expect(config.config!.fsrsParams6).toEqual(Array(21).fill(2));
        expect(config.config!.rwkvReviewMinElapsedSecs).toBe(30);
        expect(config.config!.fsrsDynamicDesiredRetentionParams).toEqual(Array(15).fill(3));
    }
    expect(out.configs![0].config!.newPerDay).toBe(10);
    expect(out.configs![0].config!.learnSteps).toEqual([1, 10]);
    expect(JSON.parse(new TextDecoder().decode(out.configs![0].config!.other))).toEqual({
        new: { separate: true },
        rev: { fuzz: 0.05, minSpace: 1 },
        fsrs7IncludeSameDayOptimize: false,
    });
    expect(out.limits!.desiredRetention).toBe(0.95);
    expect(state.getConfigById(1618570764780n)!.config!.other).toEqual(source.other);
    expect(await state.isModified()).toBe(true);
});

test("copied retention stays an unsaved change after first selecting the destination preset", async () => {
    const data = new DeckConfigsForUpdate(exampleData);
    data.allConfig[0].config!.config!.desiredRetention = 0.8;
    data.allConfig[1].config!.config!.desiredRetention = 0.9;
    const state = new DeckOptionsState(123n, data);
    state.resolveOriginalConfigs();
    expect(await state.isModified()).toBe(false);
    state.applyScheduleToAllPresets({ scheduler: false, fallback: false, settings: false, retention: true });
    state.setCurrentIndex(1);
    await state.markCurrentPresetAsLoaded();
    expect(get(state.currentConfig).desiredRetention).toBe(0.9);
    expect(await state.isModified()).toBe(true);
    expect(state.dataForSaving(UpdateDeckConfigsMode.NORMAL).configs!.map((config) => config.name))
        .toEqual(["Default"]);
});

test("applying no categories leaves other presets out of the save request", async () => {
    const state = startingState();
    state.resolveOriginalConfigs();
    state.applyScheduleToAllPresets({ scheduler: false, fallback: false, settings: false, retention: false });
    expect(await state.isModified()).toBe(false);
    expect(state.dataForSaving(UpdateDeckConfigsMode.NORMAL).configs!.map((config) => config.name))
        .toEqual(["another one"]);
});
