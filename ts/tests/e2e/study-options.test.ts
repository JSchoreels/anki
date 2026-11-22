// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { OpChanges } from "@generated/anki/collection_pb";
import { DeckConfigsForUpdate, UpdateDeckConfigsRequest } from "@generated/anki/deck_config_pb";
import { SetSettingJsonRequest } from "@generated/anki/frontend_pb";
import { Json, String as GenericString } from "@generated/anki/generic_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

test("study options preview the draft, retain it across layouts, and save the weekday load", async ({ page }) => {
    let classic = false;
    await page.setViewportSize({ width: 1100, height: 950 });
    await page.route("**/_anki/getProfileConfigJson", async (route) => {
        if (decodeRequestBody(route.request(), GenericString).val !== "deckOptionsClassicView") {
            return route.fallback();
        }
        await route.fulfill({ body: Buffer.from(new Json({ json: Buffer.from(JSON.stringify(classic)) }).toBinary()) });
    });
    await page.route("**/_anki/setProfileConfigJson", async (route) => {
        const setting = decodeRequestBody(route.request(), SetSettingJsonRequest);
        if (setting.key !== "deckOptionsClassicView") {
            return route.fallback();
        }
        classic = JSON.parse(new TextDecoder().decode(setting.valueJson));
        await route.fulfill({ body: Buffer.alloc(0) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });
    await page.goto("/deck-options/1#night");
    const preview = page.getByRole("region", { name: "Session Preview" });
    await expect(preview).toBeVisible();
    await page.getByRole("combobox", { name: "Preview cards from" }).click();
    await page.getByRole("option", { name: "Example · 120 new cards" }).click();
    await page.getByRole("spinbutton", { name: "New cards/day", exact: true }).fill("5");
    await expect(preview.getByRole("button", { name: /Postponed.*N006/ })).toBeVisible();
    const reasons = preview.getByRole("list", { name: "Why this order?" });
    await expect(reasons.getByText("Beyond today’s new-card limit.", { exact: false })).toBeVisible();
    const lines = await reasons.getByRole("listitem").count();
    await preview.getByRole("button", { name: /Postponed.*N006/ }).click();
    await expect(reasons.getByRole("listitem")).toHaveCount(lines);
    await expect(preview.getByRole("button", { name: /Postponed.*N006/ })).toHaveAttribute("aria-pressed", "true");
    await expect(reasons.getByText("N006", { exact: true }).locator("..")).toHaveAttribute("aria-current", "true");
    await expect(page.getByRole("spinbutton", { name: "New cards/day", exact: true })).toHaveCSS(
        "border-bottom-style",
        "dotted",
    );
    await page.screenshot({ path: test.info().outputPath("deck-options-preview.jpg"), type: "jpeg", quality: 85 });
    const wideCards = await preview.getByRole("button").count();
    await page.setViewportSize({ width: 460, height: 900 });
    await expect.poll(() => preview.getByRole("button").count()).toBeLessThan(wideCards);
    await expect(preview.getByRole("button", { name: /Postponed.*N006/ })).toBeVisible();
    const friday = page.getByRole("slider", { name: "Fri", exact: true });
    await friday.focus();
    await friday.press("Home");
    await friday.press("ArrowUp");
    await expect(friday).toHaveAttribute("aria-valuetext", "25%");
    await expect(page.getByRole("heading", { level: 1 }).last()).toHaveText("Easy Days");
    const layout = page.getByRole("switch", { name: "Classic view" });
    await layout.check();
    await expect.poll(() => classic).toBe(true);
    await expect(page.getByRole("heading", { name: "Display Order", exact: true })).toBeVisible();
    await expect(preview).toHaveCount(0);
    await layout.uncheck();
    await expect.poll(() => classic).toBe(false);
    await expect(page.getByRole("spinbutton", { name: "New cards/day", exact: true })).toHaveValue("5");
    await expect(friday).toHaveAttribute("aria-valuetext", "25%");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => saved?.configs.at(-1)?.config?.easyDaysPercentages[4]).toBe(0.25);
});

test("show after reviews keeps new-card slots when the review limit is reached", async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto("/deck-options/1");
    const preview = page.getByRole("region", { name: "Session Preview" });
    await page.getByRole("combobox", { name: "Preview cards from" }).click();
    await page.getByRole("option", { name: "Example · 120 new cards" }).click();
    await page.getByRole("combobox", { name: "Show new cards" }).click();
    await page.getByRole("option", { name: "Show after reviews" }).click();
    const ignore = page.getByRole("checkbox", { name: "New cards ignore review limit" });
    const newLimit = page.getByRole("spinbutton", { name: "New cards/day", exact: true });
    const reviewLimit = page.getByRole("spinbutton", { name: "Maximum reviews/day" });
    await ignore.check();
    await newLimit.fill("100");
    await reviewLimit.fill("100");
    const counts = async () =>
        (await preview.locator("[aria-live=polite]").innerText())
            .replace(/\p{Cf}/gu, "").replace(/\s+/g, " ").trim();
    await expect.poll(counts).toBe(
        "104 cards in the current queue · 2 reviews · 100 new-card slots · 2 learning steps",
    );
    await expect(preview.getByText("New cards follow the reviews that fit today’s limit", { exact: false }))
        .toBeVisible();
    const lastRank = preview.getByRole("list", { name: "Why this order?" }).getByText("104", { exact: true });
    await expect(lastRank).toBeVisible();
    expect(
        await lastRank.evaluate((node) => {
            const range = document.createRange();
            range.selectNodeContents(node);
            return range.getClientRects().length;
        }),
    ).toBe(1);
    await newLimit.fill("2");
    await reviewLimit.fill("0");
    await expect.poll(counts).toBe(
        "3 cards in the current queue · 0 reviews · 2 new-card slots · 1 learning step",
    );
    await expect(preview.getByRole("button", { name: /2 · New.*N001/ })).toBeVisible();
    await expect(preview.getByRole("button", { name: /3 · New.*N002/ })).toBeVisible();
    await expect(preview.getByRole("button", { name: /Postponed.*N003/ })).toBeVisible();
    await ignore.uncheck();
    await expect.poll(counts).toContain("0 new-card slots");
    await expect(preview.getByText("Today’s review limit also applies to new cards.", { exact: false })).toBeVisible();
    await ignore.check();
    await reviewLimit.fill("100");
    await page.setViewportSize({ width: 400, height: 900 });
    await expect.poll(counts).toContain("2 new-card slots");
    await expect(preview.getByRole("button", { name: /New.*N001/ })).toBeVisible();
    await expect(preview.getByRole("button", { name: /Postponed.*N003/ })).toBeVisible();
});

test("a numeric field orders the whole mixed pool or each priority group", async ({ page }) => {
    await page.setViewportSize({ width: 1500, height: 950 });
    await page.goto("/deck-options/1");
    const choose = async (control: string, option: string) => {
        await page.getByRole("combobox", { name: control, exact: true }).click();
        await page.getByRole("option", { name: option, exact: true }).click();
    };
    await choose("Preview cards from", "Example · learning steps");
    await choose("Cards to select", "Mix new and suspended cards");
    await choose("Select by", "Note field");
    await choose("Note field", "Back");
    await choose("Sort direction", "Descending");
    const pool = page.getByRole("region", { name: "Session Preview" })
        .getByRole("button").filter({ hasText: /N00[1-3]|S01/ }).locator("strong");
    await expect(pool).toHaveText(["N002", "N003", "S01", "N001"]);
    await choose("Cards to select", "Suspended before New");
    await expect(pool).toHaveText(["S01", "N002", "N003", "N001"]);
    await choose("Cards to select", "New before Suspended");
    await expect(pool).toHaveText(["N002", "N003", "N001", "S01"]);
});

test("RWKV-Instant study layout saves new cards ignoring the minimum from Daily Limits", async ({ page }) => {
    await page.route("**/_anki/getDeckConfigsForUpdate", async (route) => {
        const response = await route.fetch();
        const data = DeckConfigsForUpdate.fromBinary(await response.body());
        const config = data.allConfig.find((entry) => entry.config!.id === data.currentDeck!.configId)!.config!.config!;
        config.rwkvReviewInstantOrderEnabled = true;
        config.rwkvReviewMinimumReviewsPerDay = 0;
        await route.fulfill({ response, body: Buffer.from(data.toBinary()) });
    });
    let saved: UpdateDeckConfigsRequest | undefined;
    await page.route("**/_anki/updateDeckConfigsAndClose", async (route) => {
        saved = decodeRequestBody(route.request(), UpdateDeckConfigsRequest);
        await route.fulfill({ body: Buffer.from(new OpChanges().toBinary()) });
    });
    await page.goto("/deck-options/1");
    const limits = page.getByRole("heading", { name: "Daily Limits", exact: true }).locator("../..");
    const rwkvCard = page.getByRole("heading", { name: "RWKV", exact: true }).locator("../..");
    const ignore = limits.getByRole("checkbox", { name: "New cards ignore minimum reviews", exact: true });
    await expect(limits.getByRole("checkbox", { name: "Also honor subdeck minimums", exact: true })).toBeEnabled();
    await expect(rwkvCard.getByRole("checkbox", { name: "Also honor subdeck minimums", exact: true }))
        .toHaveCount(0);
    await expect(ignore).toBeDisabled();
    await limits.getByRole("spinbutton", { name: "Minimum reviews per day", exact: true }).fill("5");
    await expect(ignore).toBeEnabled();
    await expect(ignore).not.toBeChecked();
    await ignore.check();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => saved?.configs.at(-1)?.config?.rwkvReviewNewCardsIgnoreMinimumReviews).toBe(true);
});

test("study layout Daily Limits help hides the RWKV entries while RWKV-Instant is off", async ({ page }) => {
    await page.goto("/deck-options/1");
    const limits = page.getByRole("heading", { name: "Daily Limits", exact: true }).locator("../..");
    await expect(limits.locator("nav button", { hasText: /^Maximum reviews\/day$/ })).toHaveCount(1);
    await expect(limits.getByRole("checkbox", { name: "New cards ignore minimum reviews", exact: true }))
        .toHaveCount(0);
    for (
        const title of ["Minimum reviews per day", "New cards ignore minimum reviews", "Also honor subdeck minimums"]
    ) {
        await expect(limits.locator("nav button", { hasText: new RegExp(`^${title}$`) })).toHaveCount(0);
    }
});
