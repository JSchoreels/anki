// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { CardStatsResponse } from "@generated/anki/stats_pb";
import type { fsrsNextInterval as fsrsNextIntervalFn } from "@generated/backend";

const S90_TARGET_RETENTION = 0.9;

type FsrsNextIntervalFn = typeof fsrsNextIntervalFn;

export async function stabilityS90ForCardInfo(
    info: CardStatsResponse | null,
    fsrsNextInterval: FsrsNextIntervalFn,
): Promise<number | null> {
    if (!info?.memoryState) {
        return null;
    }

    const response = await fsrsNextInterval({
        cardId: info.cardId,
        stability: info.memoryState.stability,
        desiredRetention: S90_TARGET_RETENTION,
    });

    return response.interval;
}
