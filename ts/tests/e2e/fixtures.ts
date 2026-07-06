// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { test as base } from "@playwright/test";

export { expect } from "@playwright/test";

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

export const test = base.extend({
    page: async ({ page, baseURL }, use) => {
        await waitForCollectionReady(baseURL);
        await use(page);
    },
});
