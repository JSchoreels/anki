// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { get, writable } from "svelte/store";

/**
 * Indicates whether an IME composition session is currently active
 */
export const isComposing = writable(false);

window.addEventListener("compositionstart", () => isComposing.set(true));
window.addEventListener("compositionend", () => isComposing.set(false));

function deepestActiveElement(root: Document | ShadowRoot): Element | null {
    const activeElement = root.activeElement;
    if (activeElement?.shadowRoot) {
        return deepestActiveElement(activeElement.shadowRoot) ?? activeElement;
    }
    return activeElement;
}

/** Commit any pending IME text before an explicit save. */
export async function commitCurrentComposition(): Promise<void> {
    if (!get(isComposing)) {
        return;
    }

    const activeElement = deepestActiveElement(document);
    if (!(activeElement instanceof HTMLElement)) {
        return;
    }

    const compositionEnded = new Promise<void>((resolve) => {
        window.addEventListener("compositionend", () => resolve(), { once: true });
    });
    activeElement.blur();
    await compositionEnded;
}
