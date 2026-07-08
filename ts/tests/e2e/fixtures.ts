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
});
