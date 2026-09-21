// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config_FsrsVersion, DeckConfigsForUpdate } from "@generated/anki/deck_config_pb";
import {
    ComputeFsrsParamsRequest,
    ComputeFsrsParamsResponse,
    EvaluateParamsLegacyRequest,
    EvaluateParamsResponse,
} from "@generated/anki/scheduler_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

for (const includeSameDayReviews of [true, false]) {
    test(`first FSRS-7 optimization evaluates defaults with same-day reviews ${includeSameDayReviews}`, async ({ page }) => {
        let defaultParams: number[] = [];
        const evaluations: EvaluateParamsLegacyRequest[] = [];
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            defaultParams = data.defaults!.config!.fsrsParams7;
            data.fsrs = true;
            data.fsrsLegacyEvaluate = true;
            data.fsrsHealthCheck = false;
            const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
                .config!.config!;
            config.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
            config.fsrsParams6 = [...data.defaults!.config!.fsrsParams6];
            config.fsrsParams7 = [];
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        await page.route("**/_anki/computeFsrsParams", async (route) => {
            const request = decodeRequestBody(route.request(), ComputeFsrsParamsRequest);
            expect(request.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SEVEN);
            expect(request.currentParams).toEqual([]);
            expect(request.includeSameDayReviews).toBe(includeSameDayReviews);
            const params = [...defaultParams];
            params[0] += 0.1;
            await route.fulfill({
                body: Buffer.from(new ComputeFsrsParamsResponse({ params, fsrsItems: 100 }).toBinary()),
            });
        });
        await page.route("**/_anki/evaluateParamsLegacy", async (route) => {
            const request = decodeRequestBody(route.request(), EvaluateParamsLegacyRequest);
            evaluations.push(request);
            await route.fulfill({
                body: Buffer.from(new EvaluateParamsResponse({ logLoss: 0.4689, rmseBins: 0.0581 }).toBinary()),
            });
        });

        await page.goto("/deck-options/1");
        const advanced = page.locator("details.fsrs-advanced");
        await advanced.locator("summary").click();
        await advanced.locator("select").selectOption({ label: "FSRS-7" });
        const parameters = page.getByRole("button", { name: "FSRS Parameters", exact: true }).locator("textarea");
        await expect(parameters).toHaveValue("");
        await page.getByRole("checkbox", { name: "Include same-day reviews in FSRS-7" }).setChecked(
            includeSameDayReviews,
        );
        await page.getByRole("button", { name: "Optimize Current Preset", exact: true }).click();
        await expect(page.getByText("Optimization Result", { exact: true })).toBeVisible();

        expect(defaultParams).toHaveLength(34);
        expect(evaluations).toHaveLength(2);
        expect(evaluations[0].params).toEqual(defaultParams);
        expect(evaluations.map((request) => request.includeSameDayReviews)).toEqual([
            includeSameDayReviews,
            includeSameDayReviews,
        ]);
        await page.getByRole("button", { name: "Keep Current", exact: true }).click();
        await expect(parameters).toHaveValue("");

        const dialog = page.waitForEvent("dialog");
        await page.getByRole("button", { name: "Evaluate", exact: true }).click();
        await (await dialog).accept();
        expect(evaluations).toHaveLength(3);
        expect(evaluations[2].params).toEqual(defaultParams);
        expect(evaluations[2].includeSameDayReviews).toBe(includeSameDayReviews);
    });
}
