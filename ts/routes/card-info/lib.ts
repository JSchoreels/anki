// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { CardStatsResponse } from "@generated/anki/stats_pb";
import * as tr2 from "@generated/ftl";
import { DAY, timeSpan, TimespanUnit, Timestamp } from "@tslib/time";

function dateString(timestamp: bigint): string {
    return new Timestamp(Number(timestamp)).dateString();
}

export interface StatsRow {
    label: string;
    value: string | number | bigint;
}

const rwkvRowLabels = [
    "RWKV computed R",
    "RWKV : Answer Button Probability",
    "RWKV Curve Next S90",
    "FSRS Next S90",
    "RWKV : R After Review",
    "RWKV : R After 10min",
    "Retrievability source",
] as const;

export function rowsFromStats(stats: CardStatsResponse): StatsRow[] {
    type ExtraRow = (typeof stats.extraRows)[number];
    const statsRows: StatsRow[] = [];
    const movedRwkvRows = new Set<ExtraRow>();

    function pushRwkvRows(): void {
        for (const label of rwkvRowLabels) {
            const row = stats.extraRows.find((row) => row.label === label);
            if (row) {
                statsRows.push(row);
                movedRwkvRows.add(row);
            }
        }
    }

    statsRows.push({ label: tr2.cardStatsAdded(), value: dateString(stats.added) });

    if (stats.firstReview != null) {
        statsRows.push({
            label: tr2.cardStatsFirstReview(),
            value: dateString(stats.firstReview),
        });
    }
    if (stats.latestReview != null) {
        statsRows.push({
            label: tr2.cardStatsLatestReview(),
            value: dateString(stats.latestReview),
        });
    }

    if (stats.dueDate != null) {
        statsRows.push({
            label: tr2.statisticsDueDate(),
            value: dateString(stats.dueDate),
        });
    }
    if (stats.duePosition != null) {
        statsRows.push({
            label: tr2.cardStatsNewCardPosition(),
            value: stats.duePosition,
        });
    }

    if (stats.interval) {
        statsRows.push({
            label: tr2.cardStatsInterval(),
            value: timeSpan(stats.interval * DAY),
        });
    }
    if (stats.memoryState) {
        let stability = timeSpan(stats.memoryState.stability * 86400, false, false);
        if (stats.memoryState.stability > 31) {
            const nativeStability = timeSpan(
                stats.memoryState.stability * 86400,
                false,
                false,
                TimespanUnit.Days,
            );
            stability += ` (${nativeStability})`;
        }
        statsRows.push({
            label: tr2.cardStatsFsrsStability(),
            value: stability,
        });
        const difficulty = (((stats.memoryState.difficulty - 1.0) / 9.0) * 100.0).toFixed(0);
        statsRows.push({
            label: tr2.cardStatsFsrsDifficulty(),
            value: `${difficulty}%`,
        });
        if (stats.fsrsRetrievability != null) {
            const retrievability = (stats.fsrsRetrievability * 100).toFixed(0);
            statsRows.push({
                label: tr2.cardStatsFsrsComputedR(),
                value: `${retrievability}%`,
            });
            if (stats.extraRows.some((row) => row.label === rwkvRowLabels[0])) {
                pushRwkvRows();
            }
        }
    } else if (stats.ease && stats.desiredRetention === undefined) {
        // Prevent showing ease when FSRS is enabled but no memory state exists.
        statsRows.push({
            label: tr2.cardStatsEase(),
            value: `${stats.ease / 10}%`,
        });
    }

    statsRows.push({ label: tr2.cardStatsReviewCount(), value: stats.reviews });
    statsRows.push({ label: tr2.cardStatsLapseCount(), value: stats.lapses });

    if (stats.totalSecs) {
        statsRows.push({
            label: tr2.cardStatsAverageTime(),
            value: timeSpan(stats.averageSecs),
        });
        statsRows.push({
            label: tr2.cardStatsTotalTime(),
            value: timeSpan(stats.totalSecs),
        });
    }

    statsRows.push({ label: tr2.cardStatsCardTemplate(), value: stats.cardType });
    statsRows.push({ label: tr2.cardStatsNoteType(), value: stats.notetype });
    let deck: string;
    if (stats.originalDeck) {
        deck = `${stats.deck} (${stats.originalDeck})`;
    } else {
        deck = stats.deck;
    }
    statsRows.push({ label: tr2.cardStatsDeckName(), value: deck });
    statsRows.push({ label: tr2.cardStatsPreset(), value: stats.preset });

    for (const row of stats.extraRows) {
        if (!movedRwkvRows.has(row)) {
            statsRows.push(row);
        }
    }

    statsRows.push({ label: tr2.cardStatsCardId(), value: stats.cardId });
    statsRows.push({ label: tr2.cardStatsNoteId(), value: stats.noteId });

    if (stats.customData) {
        let value: string;
        try {
            const obj = JSON.parse(stats.customData);
            value = Object.entries(obj)
                .map(([k, v]) => `${k}=${v}`)
                .join(" ");
        } catch (exc) {
            value = stats.customData;
        }
        statsRows.push({
            label: tr2.cardStatsCustomData(),
            value: value,
        });
    }

    return statsRows;
}
