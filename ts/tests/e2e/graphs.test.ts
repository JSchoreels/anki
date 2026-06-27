// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const largeSeedCount = Number(process.env.ANKI_E2E_SEED_REVIEW_CARDS || 0);
const fakeRwkvBackendEnabled = process.env.ANKI_E2E_FAKE_RWKV_BACKEND === "1";
const graphDebugPath = "/graphs?currentDeckId=1&graphDebug=1";

function graphResponsePromise(page: Page) {
    return page.waitForResponse((response) => {
        return response.request().method() === "POST"
            && new URL(response.url()).pathname === "/_anki/graphs";
    });
}

function collectGraphConsoleMessages(page: Page): string[] {
    const graphConsoleMessages: string[] = [];
    page.on("console", (message) => {
        const text = message.text();
        if (text.includes("graphs")) {
            graphConsoleMessages.push(`[${message.type()}] ${text}`);
        }
    });

    return graphConsoleMessages;
}

async function expectGraphConsoleMessage(
    graphConsoleMessages: string[],
    expected: string,
): Promise<void> {
    await expect.poll(() => {
        return graphConsoleMessages.some((message) => message.includes(expected));
    }).toBeTruthy();
}

test("graphs page clears loading state after graph data arrives", async ({ page }) => {
    const graphConsoleMessages = collectGraphConsoleMessages(page);
    const responsePromise = graphResponsePromise(page);

    await page.goto(graphDebugPath);
    await expect(page.locator("#statisticsSearchText")).toBeVisible();

    const graphResponse = await responsePromise;
    expect(graphResponse.ok()).toBeTruthy();

    await expect(page.locator(".spin.loading")).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Today" })).toBeVisible();
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs postProto decoded");
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs data applied");

    await test.info().attach("graphs-console", {
        body: graphConsoleMessages.join("\n") || "(no graph console messages)",
        contentType: "text/plain",
    });
});

test("seeded RWKV graphs page clears loading after bulk stats scoring", async ({ page }) => {
    test.skip(
        largeSeedCount <= 0 || !fakeRwkvBackendEnabled,
        "requires ANKI_E2E_SEED_REVIEW_CARDS and ANKI_E2E_FAKE_RWKV_BACKEND=1",
    );
    test.setTimeout(120_000);

    const graphConsoleMessages = collectGraphConsoleMessages(page);
    const responsePromise = graphResponsePromise(page);
    const start = Date.now();

    await page.goto(graphDebugPath);
    await expect(page.locator("#statisticsSearchText")).toBeVisible();

    const graphResponse = await responsePromise;
    const responseElapsedMs = Date.now() - start;
    expect(graphResponse.ok()).toBeTruthy();

    await expect(page.locator(".spin.loading")).toHaveCount(0, { timeout: 15_000 });
    const clearElapsedMs = Date.now() - start;
    await expect(page.getByRole("heading", { name: "Card Counts" })).toBeVisible();
    await expect(page.getByRole("row", { name: /Total\s+10,000/ })).toBeVisible();
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs postProto decoded");
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs data applied");

    console.log(
        `seeded RWKV graphs loaded: cards=${largeSeedCount} response_elapsed_ms=${responseElapsedMs} clear_elapsed_ms=${clearElapsedMs}`,
    );

    await test.info().attach("seeded-rwkv-graphs-console", {
        body: graphConsoleMessages.join("\n") || "(no graph console messages)",
        contentType: "text/plain",
    });
});
