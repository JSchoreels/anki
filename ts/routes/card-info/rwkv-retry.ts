// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { CardStatsResponse } from "@generated/anki/stats_pb";

const retryDelays = [500, 1000, 2000, 4000, 8000];
type RetryStats = Pick<CardStatsResponse, "cardId" | "rwkvRetryPending">;

export function createRwkvCardInfoRetry(refresh: () => Promise<unknown>) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cardIds = "";
    let attempts = 0;
    let stopped = false;

    function cancelTimer(): void {
        clearTimeout(timer);
        timer = undefined;
    }

    return {
        update(stats: readonly (RetryStats | null)[]): void {
            cancelTimer();
            if (stopped) {
                return;
            }
            const ids = stats.map((card) => card?.cardId.toString() ?? "").join(",");
            if (ids !== cardIds) {
                cardIds = ids;
                attempts = 0;
            }
            if (!stats.some((card) => card?.rwkvRetryPending)) {
                attempts = 0;
                return;
            }
            const delay = retryDelays[attempts];
            if (delay === undefined) {
                return;
            }
            timer = setTimeout(() => {
                timer = undefined;
                attempts++;
                void refresh().catch(console.error);
            }, delay);
        },
        stop(): void {
            stopped = true;
            cancelTimer();
        },
    };
}
