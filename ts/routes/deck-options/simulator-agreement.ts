// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { RwkvComparisonResponse, SimulateFsrsWorkloadResponse } from "@generated/anki/scheduler_pb";

export type Agreement = "fsrs" | "rwkv" | "mixed" | "unavailable";

export interface AgreementCell {
    retention: number;
    agreement: Agreement;
    // Rows are answer models, columns are memory estimators (FSRS, RWKV).
    changes: (number | undefined)[][];
}

export interface AgreementRun {
    run: number;
    preset: string;
    days: number;
    graphLearner: number;
    fsrsAnswers: RwkvComparisonResponse;
    rwkvAnswers: RwkvComparisonResponse;
}

function efficiency(
    response: SimulateFsrsWorkloadResponse | undefined,
    retention: number,
): number | undefined {
    if (!response) {
        return undefined;
    }
    const memorized = response.memorized[retention];
    const cost = response.cost[retention];
    const baseline = response.reviewlessEndMemorized;
    if (
        !Number.isFinite(memorized)
        || !Number.isFinite(cost)
        || !Number.isFinite(baseline)
        || cost <= 0
    ) {
        return undefined;
    }
    return 3600 * (memorized - baseline) / cost;
}

function change(
    fsrs: SimulateFsrsWorkloadResponse | undefined,
    rwkv: SimulateFsrsWorkloadResponse | undefined,
    retention: number,
): number | undefined {
    const fsrsEfficiency = efficiency(fsrs, retention);
    const rwkvEfficiency = efficiency(rwkv, retention);
    // A percentage relative to zero or negative FSRS efficiency is misleading.
    if (fsrsEfficiency === undefined || fsrsEfficiency <= 0 || rwkvEfficiency === undefined) {
        return undefined;
    }
    return (rwkvEfficiency - fsrsEfficiency) / fsrsEfficiency;
}

export function agreementCells(run: AgreementRun): AgreementCell[] {
    const learners = [run.fsrsAnswers, run.rwkvAnswers];
    const targets = new Set<number>();
    for (const learner of learners) {
        for (
            const score of [
                learner.fsrsScoredByFsrs,
                learner.rwkvScoredByFsrs,
                learner.fsrsScoredByRwkv,
                learner.rwkvScoredByRwkv,
            ]
        ) {
            for (const retention of Object.keys(score?.memorized ?? {})) {
                targets.add(Number(retention));
            }
        }
    }

    return [...targets].sort((a, b) => a - b).map((retention) => {
        const changes = learners.map((learner) => [
            change(learner.fsrsScoredByFsrs, learner.rwkvScoredByFsrs, retention),
            change(learner.fsrsScoredByRwkv, learner.rwkvScoredByRwkv, retention),
        ]);
        const values = changes.flat();
        let agreement: Agreement = "unavailable";
        if (values.every((value): value is number => value !== undefined)) {
            // Differences that display as 0.0% do not establish a winner.
            if (values.every((value) => value >= 0.0005)) {
                agreement = "rwkv";
            } else if (values.every((value) => value <= -0.0005)) {
                agreement = "fsrs";
            } else {
                agreement = "mixed";
            }
        }
        return { retention, agreement, changes };
    });
}
