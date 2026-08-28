// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { type Page, test as base } from "@playwright/test";

export { expect } from "@playwright/test";

interface AnkiFixtures {
    /** Page navigated to /editor/?mode=add with bridgeCommand stubbed. */
    editorPage: Page;
    /**
     * editorPage after loadNote({ initial: true }) has resolved and the first
     * field container is visible. Suitable for all editor interaction tests.
     */
    editor: Page;
    /**
     * editorPage with a second NoteEditor mounted via setupEditor("add", true)
     * — the way the legacy Qt editor loads it (editor_legacy.py) — and
     * seeded with two fields the way Python drives the webview (editor_legacy.py).
     */
    legacyEditor: Page;
}

async function waitForCollectionReady(baseURL: string | undefined): Promise<void> {
    if (!baseURL) {
        throw new Error("Playwright baseURL is required for Anki e2e tests");
    }

    const readyURL = new URL("/_anki/getDeckNames", baseURL).toString();
    const deadline = Date.now() + 30_000;
    let lastStatus = 0;

    while (Date.now() < deadline) {
        try {
            const response = await fetch(readyURL, {
                method: "POST",
                headers: { "Content-Type": "application/binary" },
                body: new Uint8Array(),
            });
            lastStatus = response.status;
            if (response.ok) {
                return;
            }
        } catch {
            lastStatus = 0;
        }

        await new Promise((resolve) => setTimeout(resolve, 250));
    }

    throw new Error(
        `Timed out waiting for Anki collection readiness; last status=${lastStatus}`,
    );
}

async function installBridgeStub(page: Page): Promise<void> {
    await page.addInitScript(() => {
        (window as any).__bridgeCalls = [];
        (window as any).bridgeCommand = (
            cmd: string,
            _callback?: (value: unknown) => void,
        ): void => {
            (window as any).__bridgeCalls.push(cmd);
        };
    });
}

export const test = base.extend<AnkiFixtures>({
    page: async ({ page, baseURL }, use) => {
        await waitForCollectionReady(baseURL);
        await use(page);
    },

    editorPage: async ({ page }, use) => {
        await installBridgeStub(page);
        await page.goto("/editor/?mode=add", { waitUntil: "domcontentloaded" });
        await page.waitForSelector(".note-editor", { timeout: 15_000 });
        await use(page);
    },

    editor: async ({ editorPage }, use) => {
        await editorPage.waitForFunction(
            () => typeof (window as any).loadNote === "function",
            { timeout: 15_000 },
        );
        await editorPage.evaluate(() => (window as any).loadNote({ initial: true }));
        await editorPage.waitForSelector(".field-container", { timeout: 15_000 });
        await use(editorPage);
    },

    legacyEditor: async ({ editorPage }, use) => {
        // The page auto-mounts a non-legacy editor; mounting a second one in
        // legacy mode overwrites the globals (setFields, loadNote,
        // require("anki/TemplateButtons"), ...) exactly like the sole
        // instance does under Qt. Snapshot setFields first so we can detect
        // when the legacy instance's onMount has replaced it.
        await editorPage.evaluate(() => {
            (window as any).__preLegacySetFields = (window as any).setFields;
            return (window as any).setupEditor("add", true);
        });
        await editorPage.waitForFunction(
            () =>
                document.querySelectorAll(".note-editor").length === 2
                && (window as any).setFields !== (window as any).__preLegacySetFields,
            { timeout: 15_000 },
        );
        // In legacy mode Python pushes note state through evaluated JS
        // instead of loadNote; replicate the same calls, in the same order.
        await editorPage.evaluate(() => {
            const w = window as any;
            w.setFields(["Front", "Back"], ["", ""]);
            w.setIsImageOcclusion(false);
            w.setNotetypeMeta({ id: 0, modTime: 0 });
            w.setCollapsed([false, false]);
            w.setClozeFields([false, false]);
            w.setPlainTexts([false, false]);
            w.setDescriptions(["", ""]);
            w.setFonts([["Arial", 20, false], ["Arial", 20, false]]);
            w.setNoteId(0);
            w.setColorButtons(["#00f", "#00f"]);
            w.setTags([]);
            w.setTagsCollapsed(false);
            w.setMathjaxEnabled(true);
            w.setShrinkImages(true);
            w.setCloseHTMLTags(true);
            w.setSticky([false, false]);
            w.triggerChanges();
        });
        await editorPage.waitForSelector(".field-container", { timeout: 15_000 });
        await use(editorPage);
    },
});
