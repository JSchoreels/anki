// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface FsrsSearchSettings {
    evaluationSearch: string;
}

const EVALUATION_SEARCH_KEY = "fsrsEvaluationSearch";

const DEFAULT_SETTINGS: FsrsSearchSettings = {
    evaluationSearch: "",
};

function readString(value: unknown, fallback: string): string {
    return typeof value === "string" ? value : fallback;
}

export function readFsrsSearchSettings(
    auxData: Record<string, unknown>,
): FsrsSearchSettings {
    return {
        evaluationSearch: readString(
            auxData[EVALUATION_SEARCH_KEY],
            DEFAULT_SETTINGS.evaluationSearch,
        ),
    };
}

export function withFsrsSearchSettings(
    auxData: Record<string, unknown>,
    settings: FsrsSearchSettings,
): Record<string, unknown> | undefined {
    const current = readFsrsSearchSettings(auxData);
    if (current.evaluationSearch === settings.evaluationSearch) {
        return;
    }

    return {
        ...auxData,
        [EVALUATION_SEARCH_KEY]: settings.evaluationSearch,
    };
}
