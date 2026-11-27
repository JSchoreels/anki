// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { CardId } from "@generated/anki/cards_pb";
import { CardStatsResponse } from "@generated/anki/stats_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

test("Card Info shows the chance of recalling within each time limit", async ({ page }, testInfo) => {
    await page.route("**/_anki/cardStats", async (route) => {
        const { cid } = decodeRequestBody(route.request(), CardId);
        const hasPrediction = cid === 123n;
        await route.fulfill({
            contentType: "application/binary",
            body: Buffer.from(new CardStatsResponse({
                cardId: cid,
                added: 1700000000n,
                extraRows: hasPrediction
                    ? [{
                        label: "RWKV : Time Under (if Good)",
                        value: "2s:10% 4s:30% 6s:50% 8s:65% 10s:75% 15s:85% 20s:90% 30s:98%",
                    }]
                    : [],
                rwkvAnswerTimeRecall: hasPrediction
                    ? [0, 2, 4, 6, 8, 10, 15, 20, 30].map((seconds, index) => ({
                        seconds,
                        recall: [0, 0.085, 0.24, 0.4, 0.515, 0.595, 0.677, 0.721, 0.782][index],
                        good: [0, 0.06, 0.18, 0.3, 0.39, 0.45, 0.51, 0.54, 0.588][index],
                        easy: [0, 0.02, 0.04, 0.065, 0.075, 0.085, 0.092, 0.096, 0.099][index],
                    }))
                    : [],
            }).toBinary()),
        });
    });

    await page.goto("/card-info/123");
    await page.addStyleTag({ url: "/_anki/css/webview.css" });
    await page.addStyleTag({
        content: "html, body { font-family: -apple-system, BlinkMacSystemFont, sans-serif !important; }",
    });
    await expect(page.getByText("RWKV : Time Under (if Good)")).toBeVisible();
    const graph = page.getByRole("img", { name: /RWKV Recall Within Answer Time/ });
    await expect(graph).toBeVisible();
    await expect(page.locator(".answer-time-recall .description")).toHaveText(
        "Chance that the next answer takes less than the time shown. Success and Hard include Hard, Good and Easy; Good includes Good and Easy.",
    );
    await expect(graph.locator("circle")).toHaveCount(36);
    await expect(graph.locator("circle.recall").first().locator("title")).toHaveText("Success 0s: 0%");
    await expect(graph.locator("circle.recall").nth(1).locator("title")).toHaveText("Success 2s: 9%");
    await expect(graph.locator("circle.hard").nth(2).locator("title")).toHaveText("Hard or better 4s: 24%");
    await expect(graph.locator("circle.good").nth(1).locator("title")).toHaveText("Good or better 2s: 8%");
    await expect(graph.locator("circle.easy").nth(1).locator("title")).toHaveText("Easy 2s: 2%");
    await expect(page.locator(".legend").getByText("Success", { exact: true })).toBeVisible();
    await expect(page.locator(".legend").getByText("Hard or better", { exact: true })).toBeVisible();
    await expect(page.locator(".legend").getByText("Good or better", { exact: true })).toBeVisible();
    await expect(page.locator(".legend").getByText("Easy", { exact: true })).toBeVisible();
    await expect(graph.getByText("Answer time limit (seconds)")).toBeVisible();
    const overflow = await page.locator(".answer-time-recall").evaluate((container) => {
        const svg = container.querySelector("svg")!;
        return svg.getBoundingClientRect().right - container.getBoundingClientRect().right;
    });
    expect(overflow).toBeLessThanOrEqual(0.5);
    await page.locator(".answer-time-recall").screenshot({ path: testInfo.outputPath("answer-time-recall.png") });
    await page.evaluate(() => document.documentElement.classList.add("night-mode"));
    await page.locator(".answer-time-recall").screenshot({ path: testInfo.outputPath("answer-time-recall-dark.png") });

    await page.goto("/card-info/124");
    await expect(page.getByRole("img", { name: /RWKV Recall Within Answer Time/ })).toHaveCount(0);
});
