// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { OpChanges } from "@generated/anki/collection_pb";
import { DeckConfigsForUpdate, UpdateDeckConfigsRequest } from "@generated/anki/deck_config_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

for (const refreshOnExit of [false, true]) {
    test(`hidden RWKV exit refresh retains saved value ${refreshOnExit}`, async ({ page }) => {
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
                .config!.config!;
            config.rwkvReviewEnabled = true;
            config.rwkvReviewInstantOrderEnabled = true;
            config.rwkvReviewRefreshOnExit = refreshOnExit;
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        let saved: UpdateDeckConfigsRequest | undefined;
        await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
            saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
            await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
        });

        await page.goto("/deck-options/1");
        await expect(page.getByRole("checkbox", { name: "Enforce Again ≤ Hard ≤ Good ≤ Easy intervals" }))
            .toBeVisible();
        await expect(page.getByText("Recommended: Use Ascending Retrievability or Random", { exact: true }))
            .toBeVisible();
        for (
            const name of [
                "Update the RWKV queue after reviewing",
                "Predict R for new cards based on creation time",
                "Dynamic Preset Addon Support",
            ]
        ) {
            await expect(page.getByRole("checkbox", { name, exact: true })).toHaveCount(0);
        }
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewRefreshOnExit).toBe(refreshOnExit);
    });
}

test("FSRS parameter unlock timing survives mounting and unmounting", async ({ page }) => {
    await page.clock.install();
    await page.goto("/deck-options/1");

    const fsrs = page.getByRole("checkbox", { name: /^FSRS\b/ });
    const advanced = page.locator("details.fsrs-advanced");
    const parameters = page.getByRole("button", { name: "FSRS Parameters", exact: true });
    const input = parameters.locator("textarea");
    await expect(fsrs).not.toBeChecked();
    await expect(parameters).toHaveCount(0);
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

    async function setTimeoutMs(ms: number): Promise<void> {
        await page.evaluate((ms) => (window as any).anki.setParameterUnlockClickTimeoutMs(ms), ms);
    }

    async function enableFsrs(): Promise<void> {
        await fsrs.check();
        await page.clock.runFor(1);
        await advanced.locator("summary").click();
    }

    async function clickThreeTimes(interval: number): Promise<void> {
        await parameters.click();
        await page.clock.runFor(interval);
        await parameters.click();
        await expect(input).toBeDisabled();
        await page.clock.runFor(interval);
        await parameters.click();
    }

    await setTimeoutMs(1000);
    const defaultMs = await page.evaluate(() => (window as any).anki.defaultParameterUnlockClickTimeoutMs);
    expect(defaultMs).toBe(500);

    // The host can configure timing before the first mount, and remounts retain it.
    for (let mount = 0; mount < 2; mount++) {
        await enableFsrs();
        await expect(input).toBeDisabled();
        await clickThreeTimes(750);
        await expect(input).toBeEnabled();
        await fsrs.uncheck();
        await expect(parameters).toHaveCount(0);
    }

    // Changing the timeout while the controls are absent applies to their next mount.
    await setTimeoutMs(2000);
    await enableFsrs();
    await clickThreeTimes(1250);
    await expect(input).toBeEnabled();
    await fsrs.uncheck();
    await enableFsrs();

    // Changes made after mounting also apply, without changing the three-click gate.
    await setTimeoutMs(defaultMs);
    await clickThreeTimes(750);
    await expect(input).toBeDisabled();
    await page.clock.runFor(defaultMs + 1);
    await clickThreeTimes(100);
    await expect(input).toBeEnabled();

    // Host preferences last for this page only; a fresh page starts at the default.
    await setTimeoutMs(2000);
    await page.reload();
    await expect(fsrs).not.toBeChecked();
    await enableFsrs();
    await clickThreeTimes(750);
    await expect(input).toBeDisabled();
});
