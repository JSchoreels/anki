// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config } from "@generated/anki/deck_config_pb";
import { cloneDeep } from "lodash-es";

import { readFsrs7SameDaySettings, withFsrs7SameDaySettings } from "./fsrs-same-day-settings";
import {
    readFsrs7SchedulingPenaltySettings,
    withFsrs7SchedulingPenaltySettings,
} from "./fsrs-scheduling-penalty-settings";
import { readFsrsSearchSettings, withFsrsSearchSettings } from "./fsrs-search-settings";
import { dueDateScheduler, scheduler, withDueDateScheduler, withScheduler } from "./scheduler-choice";

export interface ScheduleCopyOptions {
    scheduler: boolean;
    fallback: boolean;
    settings: boolean;
    retention: boolean;
}

export function defaultScheduleCopyOptions(): ScheduleCopyOptions {
    return { scheduler: true, fallback: true, settings: true, retention: true };
}

// Preset model settings only: collection-wide controls already apply to every preset.
const modelSettingFields = [
    "fsrsParams4",
    "fsrsParams5",
    "fsrsParams6",
    "fsrsParams7",
    "fsrsMinimumIntervalSecs",
    "maximumReviewInterval",
    "historicalRetention",
    "ignoreRevlogsBeforeDate",
    "paramSearch",
    "fsrsDynamicDesiredRetentionEnabled",
    "fsrsDynamicDesiredRetentionClamp",
    "fsrsDynamicDesiredRetentionParams",
    "fsrsDynamicDesiredRetentionWeights",
    "fsrsDynamicDesiredRetentionAvgDrs",
    "fsrsDynamicDesiredRetentionMin",
    "fsrsDynamicDesiredRetentionMax",
    "fsrsDynamicDesiredRetentionFsrsEqWeights",
    "fsrsDynamicDesiredRetentionFsrsEqDrs",
    "fsrsDynamicDesiredRetentionFixedTargetWeights",
    "fsrsDynamicDesiredRetentionFixedTargetDrs",
    "rwkvReviewEnforceGradeOrder",
    "rwkvReviewMinimumReviewsPerDay",
    "rwkvReviewCandidateRefreshEnabled",
    "rwkvReviewRefreshInterval",
    "rwkvReviewMinInterveningReviews",
    "rwkvReviewMinElapsedSecs",
] as const satisfies readonly (keyof DeckConfig_Config)[];

export function withCopiedSchedule(
    target: DeckConfig_Config,
    source: DeckConfig_Config,
    options: ScheduleCopyOptions,
): DeckConfig_Config {
    let updated = options.scheduler
        ? withScheduler(target, scheduler(source))
        : new DeckConfig_Config(target);
    // Non-Instant presets have no separate fallback; their review scheduler owns due dates.
    if (options.fallback && updated.rwkvReviewInstantOrderEnabled) {
        updated = withDueDateScheduler(updated, dueDateScheduler(source));
    }
    if (options.settings) {
        Object.assign(updated, Object.fromEntries(modelSettingFields.map((key) => [key, cloneDeep(source[key])])));
    }
    if (options.retention) {
        updated.desiredRetention = source.desiredRetention;
    }
    return updated;
}

export function withCopiedScheduleAuxData(
    target: Record<string, unknown>,
    source: Record<string, unknown>,
): Record<string, unknown> {
    let updated = withFsrs7SameDaySettings(target, readFsrs7SameDaySettings(source)) ?? target;
    updated = withFsrs7SchedulingPenaltySettings(updated, readFsrs7SchedulingPenaltySettings(source)) ?? updated;
    return withFsrsSearchSettings(updated, readFsrsSearchSettings(source)) ?? updated;
}
