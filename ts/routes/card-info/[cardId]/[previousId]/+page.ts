// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { cardStats, fsrsNextInterval } from "@generated/backend";

import { stabilityS90ForCardInfo } from "../../fsrs-stability";

import type { PageLoad } from "./$types";

function optionalBigInt(x: any): bigint | null {
    try {
        return BigInt(x);
    } catch (e) {
        return null;
    }
}

export const load = (async ({ params }) => {
    const currentId = optionalBigInt(params.cardId);
    const currentInfo = currentId !== null ? await cardStats({ cid: currentId }) : null;
    const previousId = optionalBigInt(params.previousId);
    const previousInfo = previousId !== null ? await cardStats({ cid: previousId }) : null;
    const [currentFsrsStabilityS90, previousFsrsStabilityS90] = await Promise.all([
        stabilityS90ForCardInfo(currentInfo, fsrsNextInterval),
        stabilityS90ForCardInfo(previousInfo, fsrsNextInterval),
    ]);
    return {
        currentInfo,
        previousInfo,
        currentFsrsStabilityS90,
        previousFsrsStabilityS90,
    };
}) satisfies PageLoad;
