// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { get, writable } from "svelte/store";
import { expect, test } from "vitest";

import useDOMMirror from "./dom-mirror";

function fragment(text: string): DocumentFragment {
    const content = document.createDocumentFragment();
    content.append(text);
    return content;
}

test("text committed immediately before blur is preserved in the field and store", () => {
    const content = writable(fragment("弁当[]"));
    const editable = document.createElement("div");
    editable.tabIndex = 0;
    document.body.append(editable);
    const { destroy } = useDOMMirror().mirror(editable, { store: content });

    try {
        editable.focus();
        // IME commits may change the DOM just before focus is lost, before
        // the MutationObserver has delivered the final text to the store.
        editable.textContent = "弁当[べんとう]";
        editable.blur();

        expect(editable.textContent).toBe("弁当[べんとう]");
        expect(get(content).textContent).toBe("弁当[べんとう]");
    } finally {
        destroy();
        editable.remove();
    }
});

test("blur applies store updates when the focused field has no pending edits", () => {
    const content = writable(fragment("original"));
    const editable = document.createElement("div");
    editable.tabIndex = 0;
    document.body.append(editable);
    const { destroy } = useDOMMirror().mirror(editable, { store: content });

    try {
        editable.focus();
        content.set(fragment("updated elsewhere"));
        editable.blur();

        expect(editable.textContent).toBe("updated elsewhere");
        expect(get(content).textContent).toBe("updated elsewhere");
    } finally {
        destroy();
        editable.remove();
    }
});
