// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { cardStats, fsrsNextInterval } from "@generated/backend";

import { stabilityS90ForCardInfo } from "../fsrs-stability";

import type { PageLoad } from "./$types";

function optionalBigInt(x: any): bigint | null {
    try {
        return BigInt(x);
    } catch (e) {
        return null;
    }
}

export const load = (async ({ params }) => {
    const cid = optionalBigInt(params.cardId);
    const info = cid !== null ? await cardStats({ cid }) : null;
    const fsrsStabilityS90 = await stabilityS90ForCardInfo(info, fsrsNextInterval);
    return { info, fsrsStabilityS90 };
}) satisfies PageLoad;
