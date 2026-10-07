// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";

const fsrsVersions = {
    "fsrs-4": DeckConfig_Config_FsrsVersion.FOUR,
    "fsrs-5": DeckConfig_Config_FsrsVersion.FIVE,
    "fsrs-6": DeckConfig_Config_FsrsVersion.SIX,
    "fsrs-7": DeckConfig_Config_FsrsVersion.SEVEN,
};

export type DueDateScheduler = keyof typeof fsrsVersions | "rwkv-curve";
export type Scheduler = DueDateScheduler | "rwkv-instant";

type SchedulerCard = "fsrs" | "rwkv";
const schedulerCards: Record<Scheduler, SchedulerCard> = {
    "fsrs-4": "fsrs",
    "fsrs-5": "fsrs",
    "fsrs-6": "fsrs",
    "fsrs-7": "fsrs",
    "rwkv-curve": "rwkv",
    "rwkv-instant": "rwkv",
};

export function dueDateScheduler(config: DeckConfig_Config): DueDateScheduler {
    if (config.rwkvReviewEnabled) {
        return "rwkv-curve";
    }
    switch (config.fsrsVersion) {
        case DeckConfig_Config_FsrsVersion.FOUR:
            return "fsrs-4";
        case DeckConfig_Config_FsrsVersion.FIVE:
            return "fsrs-5";
        case DeckConfig_Config_FsrsVersion.SIX:
            return "fsrs-6";
        default:
            return "fsrs-7";
    }
}

export function scheduler(config: DeckConfig_Config): Scheduler {
    return config.rwkvReviewInstantOrderEnabled ? "rwkv-instant" : dueDateScheduler(config);
}

export function schedulerCardOrder(config: DeckConfig_Config): SchedulerCard[] {
    const review = scheduler(config);
    const selected = review === "rwkv-instant" ? [review, dueDateScheduler(config)] : [review];
    // Keep inactive cards mounted: each owns its visibility and retains its editing state.
    return [
        ...new Set<SchedulerCard>([
            ...selected.map((model) => schedulerCards[model]),
            ...Object.values(schedulerCards),
        ]),
    ];
}

export function withDueDateScheduler(config: DeckConfig_Config, choice: DueDateScheduler): DeckConfig_Config {
    const updated = new DeckConfig_Config(config);
    updated.rwkvReviewEnabled = choice === "rwkv-curve";
    if (choice !== "rwkv-curve") {
        updated.fsrsVersion = fsrsVersions[choice];
    }
    return updated;
}

export function withScheduler(config: DeckConfig_Config, choice: Scheduler): DeckConfig_Config {
    // Instant keeps the existing interval scheduler as its due-date companion.
    const updated = choice === "rwkv-instant"
        ? new DeckConfig_Config(config)
        : withDueDateScheduler(config, choice);
    updated.rwkvReviewInstantOrderEnabled = choice === "rwkv-instant";
    return updated;
}
