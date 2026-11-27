// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { getDeckConfigsForUpdate } from "@generated/backend";
import { getProfileConfig } from "@tslib/profile";

import { DeckOptionsState } from "../lib";
import type { PageLoad } from "./$types";

export const load = (async ({ params }) => {
    const deckId = Number(params.deckId);

    const did = BigInt(deckId);
    const [info, classic] = await Promise.all([
        getDeckConfigsForUpdate({ did }),
        getProfileConfig("deckOptionsClassicView"),
    ]);
    const state = new DeckOptionsState(BigInt(did), info);

    return { state, classic: classic === true };
}) satisfies PageLoad;
