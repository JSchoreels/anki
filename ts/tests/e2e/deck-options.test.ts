// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { OpChanges } from "@generated/anki/collection_pb";
import {
    DeckConfig,
    DeckConfig_Config,
    DeckConfig_Config_FsrsVersion,
    DeckConfig_Config_ReviewMix as ReviewMix,
    DeckConfigsForUpdate,
    DeckConfigsForUpdate_ConfigWithExtra,
    DeckConfigsForUpdate_CurrentDeck_Limits,
    GetRetentionWorkloadResponse,
    RwkvProfileSettings,
    UpdateDeckConfigsRequest,
} from "@generated/anki/deck_config_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

test(
    "RWKV Advanced hides model, queue and comparison controls and preserves precision on Save",
    async ({ page }, testInfo) => {
        let saved: UpdateDeckConfigsRequest | undefined;
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!.config!
                .config!;
            config.rwkvReviewInstantOrderEnabled = true;
            config.rwkvReviewEnabled = true;
            config.rwkvReviewMinimumReviewsPerDay = 100;
            data.rwkvProfileSettings = new RwkvProfileSettings(
                saved?.rwkvProfileSettings ?? {
                    model: "legacy",
                    statePrecision: "int8",
                },
            );
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
            saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
            await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
        });
        await page.goto("/deck-options/1");
        await page.addStyleTag({ url: "/_anki/css/webview.css" });
        await page.addStyleTag({
            content: "html, body { font-family: -apple-system, BlinkMacSystemFont, sans-serif !important; }",
        });
        const card = page.getByRole("heading", { name: "RWKV", exact: true }).locator("../..");
        const advanced = card.locator("details.rwkv-advanced");
        const openAdvanced = advanced.locator("summary");
        const group = advanced.getByRole("region", { name: "Prediction model", exact: true });
        const model = group.getByRole("checkbox", { name: "Use Smaller Model", exact: true });
        const compare = advanced.getByRole("button", { name: "Compare RWKV with FSRS", exact: true });
        await expect(model).toBeHidden();
        await expect(compare).toBeHidden();
        await expect(advanced.getByText("Use faster, approximate queue updates", { exact: true })).toBeHidden();
        await expect(advanced.getByText("Update the RWKV queue every", { exact: true })).toBeHidden();
        await expect(card.getByText("Storage precision", { exact: true })).toHaveCount(0);
        await expect(card.getByText("Recommended: Use Ascending Retrievability or Random", { exact: true }))
            .toHaveCount(0);
        await expect(card.getByRole("button", { name: "Build Cache", exact: true })).toBeVisible();
        await page.setViewportSize({ width: 736, height: 1000 });
        await card.screenshot({ path: testInfo.outputPath("rwkv-options-collapsed.png") });
        await page.setViewportSize({ width: 1280, height: 900 });
        await openAdvanced.focus();
        await page.keyboard.press("Enter");
        await expect(model).not.toBeChecked();
        await expect(group.getByText("Legacy - Bigger Model", { exact: true })).toHaveCount(0);
        await expect(compare).toBeVisible();
        await compare.click();
        const comparison = page.getByRole("dialog").filter({
            has: page.getByRole("heading", { name: "FSRS / RWKV Efficiency Comparison (Experimental)", exact: true }),
        });
        await expect(comparison).toBeVisible();
        await comparison.getByRole("button", { name: "Close", exact: true }).click();
        await expect(comparison).toBeHidden();
        await group.getByRole("button", { name: "About prediction models", exact: true }).click();
        await expect(group).toContainText("Applies to all decks in this profile on this computer");
        await expect(group.getByRole("link", { name: "Model Factory results" })).toHaveAttribute(
            "href",
            "https://jschoreels.github.io/anki-rwkv/factory.html",
        );
        await expect(group.getByRole("link", { name: "Matched backup comparison" })).toHaveAttribute(
            "href",
            "https://github.com/JSchoreels/anki/blob/main/docs/rwkv-four-model-quality-2026-10-22.MD",
        );
        await model.check();
        expect(saved).toBeUndefined();
        await page.reload();
        await openAdvanced.click();
        await expect(model).not.toBeChecked();
        await model.check();
        await group.getByRole("button", { name: "About prediction models", exact: true }).click();
        await card.screenshot({ path: testInfo.outputPath("rwkv-options-advanced.png") });
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => saved?.rwkvProfileSettings).toMatchObject({
            model: "sm55",
            statePrecision: "int8",
        });
        await page.reload();
        await openAdvanced.click();
        await expect(model).toBeChecked();
        await model.uncheck();
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => saved?.rwkvProfileSettings).toMatchObject({ model: "legacy", statePrecision: "int8" });
        await page.reload();
        await openAdvanced.click();
        await expect(model).not.toBeChecked();
    },
);

for (const model of ["sm40", "sm36"]) {
    test(`saving older RWKV model ${model} chooses SM55 without changing hidden precision`, async ({ page }) => {
        let saved: UpdateDeckConfigsRequest | undefined;
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
                .config!.config!.rwkvReviewInstantOrderEnabled = true;
            data.rwkvProfileSettings = new RwkvProfileSettings(
                saved?.rwkvProfileSettings ?? { model, statePrecision: "f32" },
            );
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
            saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
            await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
        });
        await page.goto("/deck-options/1");
        await page.reload();
        expect(saved).toBeUndefined();
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => saved?.rwkvProfileSettings).toMatchObject({ model: "sm55", statePrecision: "f32" });
        await page.reload();
        await page.locator("details.rwkv-advanced summary").click();
        await expect(page.getByRole("checkbox", { name: "Use Smaller Model", exact: true })).toBeChecked();
    });
}

test("all intraday orders save and reload without changing descending R or other metadata", async ({ page }) => {
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = true;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!;
        config.config!.rwkvReviewEnabled = false;
        config.config!.rwkvReviewInstantOrderEnabled = false;
        config.config!.reviewOrder = 11;
        config.config!.other = saved?.configs.at(-1)?.config?.other ?? new TextEncoder().encode(JSON.stringify({
            "jschoreels.deckoptions": { future_option: "preserve" },
            addon: { value: 42 },
        }));
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });

    await page.goto("/deck-options/1");
    const selector = page.locator(".setting-title", { hasText: "Intraday learning/review order" })
        .locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' row ')][1]")
        .getByRole("combobox");
    await expect(selector).toHaveText("Mix with reviews");
    for (
        const [mix, label] of [
            [ReviewMix.BEFORE_REVIEWS, "Show before reviews"],
            [ReviewMix.AFTER_REVIEWS, "Show after reviews"],
            [ReviewMix.MIX_WITH_REVIEWS, "Mix with reviews"],
        ] as const
    ) {
        await selector.click();
        await page.getByRole("option", { name: label, exact: true }).click();
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => {
            const config = saved?.configs.at(-1)?.config;
            return config && JSON.parse(new TextDecoder().decode(config.other))["jschoreels.deckoptions"];
        }).toEqual({ future_option: "preserve", intraday_learning_mix: mix });
        const config = saved!.configs.at(-1)!.config!;
        expect(config.reviewOrder).toBe(11);
        expect(JSON.parse(new TextDecoder().decode(config.other)).addon).toEqual({ value: 42 });
        await page.reload();
        await expect(selector).toHaveText(label);
    }
});

test(
    "apply schedule dialog defaults to every category and only saves selected categories",
    async ({ page }, testInfo) => {
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            data.fsrs = true;
            data.currentDeck!.configId = 1n;
            data.currentDeck!.limits = new DeckConfigsForUpdate_CurrentDeck_Limits({ desiredRetention: 0.95 });
            data.allConfig = [
                new DeckConfigsForUpdate_ConfigWithExtra({
                    config: new DeckConfig({
                        id: 1n,
                        name: "Source",
                        config: new DeckConfig_Config({
                            ...data.defaults!.config!,
                            rwkvReviewInstantOrderEnabled: true,
                            rwkvReviewEnabled: false,
                            fsrsVersion: DeckConfig_Config_FsrsVersion.SIX,
                            desiredRetention: 0.9,
                            rwkvReviewMinElapsedSecs: 60,
                        }),
                    }),
                    useCount: 1,
                }),
                new DeckConfigsForUpdate_ConfigWithExtra({
                    config: new DeckConfig({
                        id: 2n,
                        name: "Other",
                        config: new DeckConfig_Config({
                            ...data.defaults!.config!,
                            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
                            rwkvReviewInstantOrderEnabled: false,
                            rwkvReviewEnabled: false,
                            desiredRetention: 0.8,
                            rwkvReviewMinElapsedSecs: 10,
                            newPerDay: 17,
                        }),
                    }),
                    useCount: 0,
                }),
            ];
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        let saved: UpdateDeckConfigsRequest | undefined;
        await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
            saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
            await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
        });
        await page.goto("/deck-options/1");
        const open = page.getByRole("button", { name: "Apply Schedule to All Presets", exact: true });
        const dialog = page.getByRole("dialog", { name: "Apply Schedule to All Presets", exact: true });
        const save = page.getByRole("button", { name: "Save", exact: true });
        await open.click();
        for (const name of ["Scheduler", "Desired Retention", "Fallback", "Settings"]) {
            await expect(dialog.getByRole("checkbox", { name, exact: true })).toBeChecked();
        }
        await dialog.screenshot({ path: testInfo.outputPath("apply-schedule-dialog.png") });
        await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
        await save.click();
        await expect.poll(() => saved?.configs.map((config) => config.name)).toEqual(["Source"]);

        await open.click();
        await expect(dialog).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await save.click();
        await expect.poll(() => saved?.configs.map((config) => config.name)).toEqual(["Source"]);

        await open.click();
        for (const name of ["Scheduler", "Desired Retention", "Fallback", "Settings"]) {
            await dialog.getByRole("checkbox", { name, exact: true }).uncheck();
        }
        await expect(dialog.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
        await dialog.getByRole("checkbox", { name: "Scheduler", exact: true }).check();
        await dialog.getByRole("button", { name: "Apply", exact: true }).click();
        await expect(dialog).toBeHidden();
        await save.click();
        await expect.poll(() => saved?.configs.map((config) => config.name)).toEqual(["Other", "Source"]);
        expect(saved!.configs[0].config!.rwkvReviewInstantOrderEnabled).toBe(true);
        expect(saved!.configs[0].config!.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SEVEN);
        expect(saved!.configs[0].config!.desiredRetention).toBeCloseTo(0.8);
        expect(saved!.configs[0].config!.rwkvReviewMinElapsedSecs).toBe(10);

        await open.click();
        for (const name of ["Scheduler", "Desired Retention", "Fallback", "Settings"]) {
            await expect(dialog.getByRole("checkbox", { name, exact: true })).toBeChecked();
        }
        await dialog.getByRole("button", { name: "Apply", exact: true }).click();
        await save.click();
        await expect.poll(() => saved?.configs[0].config?.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SIX);
        expect(saved!.configs[0].config!.desiredRetention).toBeCloseTo(0.9);
        expect(saved!.configs[0].config!.rwkvReviewMinElapsedSecs).toBe(60);
        expect(saved!.configs[0].config!.newPerDay).toBe(17);
        expect(saved!.limits!.desiredRetention).toBeCloseTo(0.95);
    },
);

test("scheduler selectors save the review and Instant due-date models", async ({ page }) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = false;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!.config!;
        config.fsrsVersion = DeckConfig_Config_FsrsVersion.SEVEN;
        config.rwkvReviewEnabled = false;
        config.rwkvReviewInstantOrderEnabled = false;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });

    await page.goto("/deck-options/1");
    const model = page.getByRole("radiogroup", { name: "Review scheduler", exact: true });
    const dueDates = page.getByRole("radiogroup", { name: "Fallback", exact: true });
    const fsrs = page.getByRole("checkbox", { name: /^Machine Learning Based scheduling\b/ });
    const schedulerCard = page.getByRole("heading", { name: "Scheduler", exact: true }).locator("../..");
    const fsrsCard = page.getByRole("heading", { name: "FSRS", exact: true }).locator("../..");
    const rwkvCard = page.getByRole("heading", { name: "RWKV", exact: true }).locator("../..");
    await expect(model.getByRole("radio", { name: "FSRS-7", exact: true })).toBeChecked();
    await expect(fsrs).not.toBeChecked();
    await model.getByRole("radio", { name: "FSRS-7", exact: true }).focus();
    await page.keyboard.press("ArrowLeft");
    await expect(model.getByRole("radio", { name: "FSRS-6", exact: true })).toBeChecked();
    await expect(fsrs).toBeChecked();

    for (
        const { choice, companion, version, curve, instant } of [
            { choice: "fsrs-6", version: DeckConfig_Config_FsrsVersion.SIX, curve: false, instant: false },
            { choice: "fsrs-7", version: DeckConfig_Config_FsrsVersion.SEVEN, curve: false, instant: false },
            { choice: "rwkv-curve", version: DeckConfig_Config_FsrsVersion.SEVEN, curve: true, instant: false },
            {
                choice: "rwkv-instant",
                companion: "rwkv-curve",
                version: DeckConfig_Config_FsrsVersion.SEVEN,
                curve: true,
                instant: true,
            },
            {
                choice: "rwkv-instant",
                companion: "fsrs-6",
                version: DeckConfig_Config_FsrsVersion.SIX,
                curve: false,
                instant: true,
            },
            {
                choice: "rwkv-instant",
                companion: "fsrs-7",
                version: DeckConfig_Config_FsrsVersion.SEVEN,
                curve: false,
                instant: true,
            },
            {
                choice: "rwkv-instant",
                companion: "rwkv-curve",
                version: DeckConfig_Config_FsrsVersion.SEVEN,
                curve: true,
                instant: true,
            },
        ]
    ) {
        await model.locator(`input[value="${choice}"]`).check();
        await expect(fsrs).toBeChecked();
        await expect(dueDates).toHaveCount(instant ? 1 : 0);
        if (companion) {
            await dueDates.locator(`input[value="${companion}"]`).check();
        }
        await expect(page.getByRole("button", { name: "Optimize Current Preset", exact: true }))
            .toHaveCount(curve ? 0 : 1);
        await expect(fsrsCard).toHaveCount(curve ? 0 : 1);
        await expect(rwkvCard).toHaveCount(curve || instant ? 1 : 0);
        await expect(page.getByRole("heading", { name: /^(Scheduler|FSRS|RWKV)$/ }))
            .toHaveText([
                "Scheduler",
                ...(curve || instant ? ["RWKV"] : []),
                ...(!curve ? ["FSRS"] : []),
            ]);
        await expect(schedulerCard.getByRole("button", { name: "Optimize Current Preset", exact: true }))
            .toHaveCount(0);
        await expect(schedulerCard.locator(".interval-preview-table")).toHaveCount(0);
        if (!curve) {
            await expect(fsrsCard.getByRole("button", { name: "Optimize Current Preset", exact: true }))
                .toBeVisible();
        }
        await expect(page.getByRole("checkbox", { name: /^Use RWKV-(Curve|Instant)\b/ })).toHaveCount(0);
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => [
            saved?.fsrs,
            saved?.configs.at(-1)?.config?.fsrsVersion,
            saved?.configs.at(-1)?.config?.rwkvReviewEnabled,
            saved?.configs.at(-1)?.config?.rwkvReviewInstantOrderEnabled,
        ]).toEqual([true, version, curve, instant]);
    }
});

test("scheduler help opens model descriptions without changing the enabled state", async ({ page }, testInfo) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = true;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!.config!;
        config.rwkvReviewInstantOrderEnabled = true;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    await page.goto("/deck-options/1");
    const card = page.getByRole("heading", { name: "Scheduler", exact: true }).locator("../..");
    // Include native webview styling, including macOS button shadows, for visual review.
    await page.addStyleTag({ url: "/_anki/css/webview.css" });
    await page.evaluate(() => document.body.classList.add("fancy", "isMac"));
    await page.addStyleTag({
        content: `
            html { font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
            button { box-shadow: 0px 1px 3px var(--border-subtle); }
        `,
    });
    const enabled = page.getByRole("checkbox", { name: /^Machine Learning Based scheduling\b/ });
    const model = card.getByRole("radio", { name: "RWKV-Instant", exact: true });
    await expect(card.getByRole("link", { name: "Read more about RWKV" })).toBeHidden();
    await card.screenshot({ path: testInfo.outputPath("scheduler-soft-outlines.png") });
    for (const width of [736, 560]) {
        await page.setViewportSize({ width, height: 900 });
        await card.screenshot({ path: testInfo.outputPath(`scheduler-${width}.png`) });
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.evaluate(() => document.documentElement.classList.add("night-mode"));
    await card.screenshot({ path: testInfo.outputPath("scheduler-dark.png"), animations: "disabled" });
    for (
        const [name, label] of [
            ["apply", "Apply Schedule to All Presets"],
            ["help", "Help: Review scheduler"],
        ]
    ) {
        const button = card.getByRole("button", { name: label, exact: true });
        await button.hover();
        await card.screenshot({ path: testInfo.outputPath(`scheduler-${name}-hover.png`), animations: "disabled" });
        await button.focus();
        await card.screenshot({ path: testInfo.outputPath(`scheduler-${name}-focus.png`), animations: "disabled" });
    }
    await page.evaluate(() => document.documentElement.classList.remove("night-mode"));

    for (const title of ["Machine Learning Based scheduling", "Review scheduler", "Fallback", "Desired retention"]) {
        const help = card.getByRole("button", { name: `Help: ${title}`, exact: true });
        await help.focus();
        await help.press("Enter");
        const modal = page.locator(".modal.show");
        const section = modal.locator(".carousel-item.active");
        await expect(section.getByRole("heading", { name: title, exact: true })).toBeVisible();
        if (title === "Machine Learning Based scheduling" || title === "Review scheduler") {
            await expect(section.getByRole("heading", { name: "RWKV", exact: true })).toBeVisible();
            await expect(section.getByRole("heading", { name: "FSRS", exact: true })).toBeVisible();
            const references = section.getByRole("list");
            await expect(references.getByRole("link", { name: "Read more about RWKV" })).toBeVisible();
            await expect(references.getByRole("link", { name: "Read more about FSRS" })).toBeVisible();
            const description = section.getByText(/^FSRS uses your review history/);
            const descriptionBox = (await description.boundingBox())!;
            const referencesBox = (await references.boundingBox())!;
            expect(referencesBox.y).toBeGreaterThan(descriptionBox.y + descriptionBox.height);
            await section.screenshot({ path: testInfo.outputPath(`help-${title}.png`), animations: "disabled" });
            await page.evaluate(() => document.documentElement.classList.add("night-mode"));
            await section.screenshot({ path: testInfo.outputPath(`help-${title}-dark.png`), animations: "disabled" });
            await page.evaluate(() => document.documentElement.classList.remove("night-mode"));
        }
        if (title === "Fallback") {
            await expect(
                section.getByText(
                    "Writes due dates for mobile sync and statistics. Instant selects reviews independently of these dates.",
                ),
            )
                .toBeVisible();
        }
        await modal.getByRole("button", { name: "OK", exact: true }).click();
        await expect(page.locator(".modal:visible")).toHaveCount(0);
        await expect(enabled).toBeChecked();
        await expect(model).toBeChecked();
    }
});

test("shared retention updates the separate FSRS card and survives model changes", async ({ page }) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = true;
        data.currentDeck!.limits!.desiredRetention = undefined;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!.config!;
        config.fsrsVersion = DeckConfig_Config_FsrsVersion.SEVEN;
        config.rwkvReviewEnabled = false;
        config.rwkvReviewInstantOrderEnabled = false;
        config.desiredRetention = 0.85;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });

    let workloadRequests = 0;
    // Keep workload simulation out of this UI binding test; interval previews use the real backend.
    await page.route("**/_anki/getRetentionWorkload", async (route) => {
        workloadRequests++;
        const response = new GetRetentionWorkloadResponse({ costs: Array(100).fill(1) });
        await route.fulfill({ body: Buffer.from(response.toBinary()) });
    });

    await page.goto("/deck-options/1");
    const schedulerCard = page.getByRole("heading", { name: "Scheduler", exact: true }).locator("../..");
    const fsrsCard = page.getByRole("heading", { name: "FSRS", exact: true }).locator("../..");
    const retention = schedulerCard.getByRole("spinbutton");
    const model = page.getByRole("radiogroup", { name: "Review scheduler", exact: true });
    await expect(retention).toHaveValue("85");
    await retention.fill("90");
    await retention.press("Tab");
    await expect(fsrsCard.getByRole("columnheader", { name: "Selected DR (90.00%)", exact: true }))
        .toBeVisible();
    await expect(fsrsCard.getByText("Approximate workload: 1.00x (vs initial DR: 85%).", { exact: true }))
        .toBeVisible();
    await expect(fsrsCard.getByText("FSRS interval-based workload only.", { exact: true })).toBeVisible();
    const requestsBeforeInstant = workloadRequests;
    await model.getByRole("radio", { name: "RWKV-Curve", exact: true }).check();
    await expect(fsrsCard).toHaveCount(0);
    await expect(retention).toHaveValue("90");
    await model.getByRole("radio", { name: "RWKV-Instant", exact: true }).check();
    await page.getByRole("radiogroup", { name: "Fallback", exact: true })
        .getByRole("radio", { name: "FSRS-6", exact: true }).check();
    await expect(fsrsCard.getByRole("columnheader", { name: "Selected DR (90.00%)", exact: true }))
        .toBeVisible();
    await expect(retention).toHaveValue("90");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(fsrsCard.getByText(/Approximate workload:/)).toHaveCount(0);
    await expect(fsrsCard.getByText("FSRS interval-based workload only.", { exact: true })).toHaveCount(0);
    await expect.poll(() => saved?.configs.at(-1)?.config?.desiredRetention).toBeCloseTo(0.9);
    await schedulerCard.getByRole("button", { name: "This deck", exact: true }).click();
    await retention.fill("92");
    await retention.press("Tab");
    await expect(fsrsCard.getByRole("columnheader", { name: "Selected DR (92.00%)", exact: true }))
        .toBeVisible();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => saved?.configs.at(-1)?.config?.desiredRetention).toBeCloseTo(0.85);
    await expect.poll(() => saved?.limits?.desiredRetention).toBeCloseTo(0.92);
    expect(workloadRequests).toBe(requestsBeforeInstant);
    await model.getByRole("radio", { name: "FSRS-7", exact: true }).check();
    await expect(fsrsCard.getByRole("columnheader", { name: "Current DR (85.00%)", exact: true }))
        .toBeVisible();
    await expect(fsrsCard.getByRole("columnheader", { name: "Selected DR (92.00%)", exact: true }))
        .toBeVisible();
    await expect(fsrsCard.getByText("Approximate workload: 1.00x (vs initial DR: 85%).", { exact: true }))
        .toBeVisible();
    expect(workloadRequests).toBe(requestsBeforeInstant);
});

test("current scheduler buttons preserve older saved models until explicitly changed", async ({ page }) => {
    let version = DeckConfig_Config_FsrsVersion.FOUR;
    let instant = false;
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = true;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!.config!;
        config.fsrsVersion = version;
        config.rwkvReviewEnabled = false;
        config.rwkvReviewInstantOrderEnabled = instant;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });

    const model = page.getByRole("radiogroup", { name: "Review scheduler", exact: true });
    const dueDates = page.getByRole("radiogroup", { name: "Fallback", exact: true });
    for (const oldVersion of [DeckConfig_Config_FsrsVersion.FOUR, DeckConfig_Config_FsrsVersion.FIVE]) {
        for (const instantEnabled of [false, true]) {
            version = oldVersion;
            instant = instantEnabled;
            await page.goto("about:blank");
            await page.goto("/deck-options/1");
            await expect(page.getByRole("button", { name: /^(Modern|Legacy)$/ })).toHaveCount(0);
            await expect(model.locator("label")).toHaveText([
                "FSRS-6",
                "FSRS-7",
                "RWKV-Curve",
                "RWKV-Instant",
            ]);
            if (instant) {
                await expect(dueDates.locator("label")).toHaveText(["FSRS-6", "FSRS-7", "RWKV-Curve"]);
                await expect(dueDates.locator("input:checked")).toHaveCount(0);
            } else {
                await expect(model.locator("input:checked")).toHaveCount(0);
                await expect(dueDates).toHaveCount(0);
            }
            await page.getByRole("button", { name: "Save", exact: true }).click();
            await expect.poll(() => [
                saved?.configs.at(-1)?.config?.fsrsVersion,
                saved?.configs.at(-1)?.config?.rwkvReviewInstantOrderEnabled,
            ]).toEqual([version, instant]);
        }
    }
    await dueDates.getByRole("radio", { name: "FSRS-6", exact: true }).check();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => saved?.configs.at(-1)?.config?.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SIX);
});

for (
    const { enteredReviews, enteredSeconds, savedReviews, savedSeconds } of [
        { enteredReviews: "7", enteredSeconds: "120", savedReviews: 7, savedSeconds: 120 },
        { enteredReviews: "0", enteredSeconds: "0", savedReviews: 0, savedSeconds: 0 },
        { enteredReviews: "10001", enteredSeconds: "86401", savedReviews: 10000, savedSeconds: 86400 },
    ]
) {
    test(`RWKV repeat spacing saves ${savedReviews} reviews and ${savedSeconds} seconds`, async ({ page }) => {
        await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
            const response = await route.fetch();
            const data = DeckConfigsForUpdate.fromBinary(await response.body());
            const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
                .config!.config!;
            config.rwkvReviewInstantOrderEnabled = true;
            config.rwkvReviewAllowSameDayReview = false;
            await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
        });
        let saved: UpdateDeckConfigsRequest | undefined;
        await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
            saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
            await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
        });

        await page.goto("/deck-options/1");
        const reviews = page.getByRole("spinbutton", { name: "Minimum other reviews before a repeat", exact: true });
        const seconds = page.getByRole("spinbutton", { name: "Minimum seconds before a repeat", exact: true });
        await expect(page.getByRole("checkbox", { name: "Allow a card to repeat on the same day", exact: true }))
            .toHaveCount(0);
        await reviews.fill(enteredReviews);
        await reviews.press("Tab");
        await seconds.fill(enteredSeconds);
        await seconds.press("Tab");
        await expect(reviews).toHaveValue(String(savedReviews));
        await expect(seconds).toHaveValue(String(savedSeconds));
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewMinInterveningReviews).toBe(savedReviews);
        await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewMinElapsedSecs).toBe(savedSeconds);
        await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewAllowSameDayReview).toBe(false);
    });
}

test("RWKV-Instant exposes and saves the shared same-day switch with FSRS off", async ({ page }) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = false;
        data.fsrsShortTermWithStepsEnabled = false;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!.config!;
        config.rwkvReviewInstantOrderEnabled = false;
        config.rwkvReviewAllowSameDayReview = false;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });

    await page.goto("/deck-options/1");
    const sameDay = page.getByRole("checkbox", { name: /^Allow same day review for \(re\)learning steps\b/ });
    const scheduler = page.getByRole("radiogroup", { name: "Review scheduler", exact: true });
    const fsrs = page.getByRole("checkbox", { name: /^Machine Learning Based scheduling\b/ });
    await expect(sameDay).toHaveCount(0);
    await scheduler.getByRole("radio", { name: "RWKV-Instant", exact: true }).check();
    await fsrs.uncheck();
    await expect(sameDay).toBeVisible();
    await expect(sameDay).not.toBeChecked();
    await sameDay.check();
    await scheduler.getByRole("radio", { name: "FSRS-7", exact: true }).check();
    await fsrs.uncheck();
    await expect(sameDay).toHaveCount(0);
    await scheduler.getByRole("radio", { name: "RWKV-Instant", exact: true }).check();
    await fsrs.uncheck();
    await expect(sameDay).toBeChecked();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => saved?.fsrsShortTermWithStepsEnabled).toBe(true);
    await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewAllowSameDayReview).toBe(false);
});

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
            .toHaveCount(0);
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

test("RWKV maintenance actions keep state building and calibration separate", async ({ page }) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        data.fsrs = true;
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!
            .config!;
        config.config!.rwkvReviewEnabled = true;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    const requests: string[] = [];
    await page.route("**/_anki/updateDeckConfigs", async (route) => {
        requests.push("save");
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });
    let finishBuild!: () => void;
    const buildFinished = new Promise<void>((resolve) => finishBuild = resolve);
    await page.route("**/_anki/forceBuildRwkvStateCache", async (route) => {
        requests.push("state");
        await buildFinished;
        await route.fulfill({ body: Buffer.alloc(0) });
    });
    let finishCalibration!: () => void;
    const calibrationFinished = new Promise<void>((resolve) => finishCalibration = resolve);
    await page.route("**/_anki/recomputeRwkvCalibrationData", async (route) => {
        requests.push("calibration");
        await calibrationFinished;
        await route.fulfill({ body: Buffer.alloc(0) });
    });
    await page.goto("/deck-options/1");
    const build = page.getByRole("button", { name: "Build Cache", exact: true });
    const calibration = page.getByRole("button", { name: "Prepare Calibration Data", exact: true });
    await build.click();
    await expect.poll(() => requests).toEqual(["save", "state"]);
    await expect(page.getByRole("button", { name: "Rebuilding RWKV State...", exact: true })).toBeDisabled();
    await expect(calibration).toBeDisabled();
    finishBuild();
    await expect(build).toBeEnabled();
    expect(requests).toEqual(["save", "state"]);

    await calibration.click();
    await expect.poll(() => requests).toEqual(["save", "state", "save", "calibration"]);
    await expect(page.getByRole("button", { name: "Preparing Calibration Data...", exact: true })).toBeDisabled();
    await expect(build).toBeDisabled();
    finishCalibration();
    await expect(calibration).toBeEnabled();
    expect(requests).toEqual(["save", "state", "save", "calibration"]);
});

test("FSRS parameter unlock timing survives mounting and unmounting", async ({ page }) => {
    await page.clock.install();
    await page.goto("/deck-options/1");

    const fsrs = page.getByRole("checkbox", { name: /^Machine Learning Based scheduling\b/ });
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
