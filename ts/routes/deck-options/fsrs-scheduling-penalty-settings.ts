// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface Fsrs7SchedulingPenaltySettings {
    enableSchedulingPenalties: boolean;
}

const KEY = "fsrs7EnableSchedulingPenalties";

const DEFAULT_SETTINGS: Fsrs7SchedulingPenaltySettings = {
    enableSchedulingPenalties: false,
};

function readBool(value: unknown, fallback: boolean): boolean {
    return typeof value === "boolean" ? value : fallback;
}

export function readFsrs7SchedulingPenaltySettings(
    auxData: Record<string, unknown>,
): Fsrs7SchedulingPenaltySettings {
    return {
        enableSchedulingPenalties: readBool(
            auxData[KEY],
            DEFAULT_SETTINGS.enableSchedulingPenalties,
        ),
    };
}

export function withFsrs7SchedulingPenaltySettings(
    auxData: Record<string, unknown>,
    settings: Fsrs7SchedulingPenaltySettings,
): Record<string, unknown> | undefined {
    const current = readFsrs7SchedulingPenaltySettings(auxData);
    if (
        current.enableSchedulingPenalties === settings.enableSchedulingPenalties
    ) {
        return;
    }

    return {
        ...auxData,
        [KEY]: settings.enableSchedulingPenalties,
    };
}
