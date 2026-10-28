// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { RwkvComparisonResponse, SimulateFsrsWorkloadResponse } from "@generated/anki/scheduler_pb";
import { expect, test } from "vitest";

import { agreementCells, type AgreementRun } from "./simulator-agreement";

function workload(gains: Record<number, number>, baseline = 10): SimulateFsrsWorkloadResponse {
    return new SimulateFsrsWorkloadResponse({
        memorized: Object.fromEntries(Object.entries(gains).map(([dr, gain]) => [dr, baseline + gain])),
        cost: Object.fromEntries(Object.keys(gains).map((dr) => [dr, 3600])),
        reviewlessEndMemorized: baseline,
    });
}

test("agreement compares both schedulers at the same retention across all four model combinations", () => {
    const fsrsScores = workload({ 50: 10, 60: 10, 70: 10, 80: 10, 90: 10, 99: 10 });
    const rwkvScores = workload({ 50: 12, 60: 8, 70: 12, 80: 10.004, 90: 12, 99: 12 });
    const alternateScores = workload({ 50: 12, 60: 8, 70: 8, 80: 12, 99: 12 }, 3);
    alternateScores.cost[99] = 0;
    const response = new RwkvComparisonResponse({
        fsrsScoredByFsrs: fsrsScores,
        rwkvScoredByFsrs: rwkvScores,
        fsrsScoredByRwkv: workload({ 50: 10, 60: 10, 70: 10, 80: 10, 90: 10, 99: 10 }, 3),
        rwkvScoredByRwkv: alternateScores,
    });
    const run: AgreementRun = {
        run: 1,
        preset: "Vocabulary",
        days: 30,
        graphLearner: 0,
        fsrsAnswers: response,
        rwkvAnswers: response,
    };

    const cells = agreementCells(run);

    expect(cells.map(({ retention, agreement }) => [retention, agreement])).toEqual([
        [50, "rwkv"],
        [60, "fsrs"],
        [70, "mixed"],
        [80, "mixed"],
        [90, "unavailable"],
        [99, "unavailable"],
    ]);
    expect(cells[0].changes.flat()).toEqual([0.2, 0.2, 0.2, 0.2]);
});

test("agreement does not present a percentage relative to zero or negative FSRS efficiency", () => {
    const fsrs = workload({ 50: 0, 60: -1, 70: 10 });
    const rwkv = workload({ 50: 1, 60: 1, 70: -1 });
    const response = new RwkvComparisonResponse({
        fsrsScoredByFsrs: fsrs,
        rwkvScoredByFsrs: rwkv,
        fsrsScoredByRwkv: fsrs,
        rwkvScoredByRwkv: rwkv,
    });
    const cells = agreementCells({
        run: 1,
        preset: "Test",
        days: 30,
        graphLearner: 0,
        fsrsAnswers: response,
        rwkvAnswers: response,
    });

    expect(cells.map((cell) => cell.agreement)).toEqual(["unavailable", "unavailable", "fsrs"]);
    expect(cells[2].changes[0][0]).toBeCloseTo(-1.1);
});
