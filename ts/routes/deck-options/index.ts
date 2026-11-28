// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/* eslint
@typescript-eslint/no-explicit-any: "off",
 */

import "$lib/sveltelib/export-runtime";
import "./deck-options-base.scss";

import { getDeckConfigsForUpdate } from "@generated/backend";
import { ModuleName, setupI18n } from "@tslib/i18n";
import { checkNightMode } from "@tslib/nightmode";
import { getProfileConfig } from "@tslib/profile";

import { modalsKey, touchDeviceKey } from "$lib/components/context-keys";
import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
import SwitchRow from "$lib/components/SwitchRow.svelte";
import TitledContainer from "$lib/components/TitledContainer.svelte";

import DeckOptionsPage from "./DeckOptionsPage.svelte";
import { DeckOptionsState } from "./lib";
import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
import SpinBoxRow from "./SpinBoxRow.svelte";

const i18n = setupI18n({
    modules: [
        ModuleName.HELP,
        ModuleName.SCHEDULING,
        ModuleName.ACTIONS,
        ModuleName.DECK_CONFIG,
        ModuleName.KEYBOARD,
        ModuleName.STUDYING,
        ModuleName.DECKS,
    ],
});

export async function setupDeckOptions(did_: number): Promise<DeckOptionsPage> {
    const did = BigInt(did_);
    const [info, classic] = await Promise.all([
        getDeckConfigsForUpdate({ did }),
        getProfileConfig("deckOptionsClassicView"),
        i18n,
    ]);

    checkNightMode();

    const context = new Map();
    context.set(modalsKey, new Map());
    context.set(touchDeviceKey, "ontouchstart" in document.documentElement);

    const state = new DeckOptionsState(BigInt(did), info);
    return new DeckOptionsPage({
        target: document.body,
        props: { state, classic: classic === true },
        context,
    });
}

export const components = {
    TitledContainer,
    SpinBoxRow,
    SpinBoxFloatRow,
    EnumSelectorRow,
    SwitchRow,
};

// if (window.location.hash.startsWith("#test")) {
//     setupDeckOptions(1);
// }
