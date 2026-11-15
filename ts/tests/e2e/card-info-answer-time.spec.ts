// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { CardId } from "@generated/anki/cards_pb";
import { CardStatsResponse } from "@generated/anki/stats_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

test("Card Info shows answer-time thresholds and the predicted recall graph", async ({ page }, testInfo) => {
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
                        value: "2s:20% 4s:40% 6s:60% 8s:70% 10s:80% 15s:90% 20s:95% 30s:99%",
                    }]
                    : [],
                rwkvAnswerTimeRecall: hasPrediction
                    ? [2, 4, 6, 8, 10, 15, 20, 30].map((seconds, index) => ({
                        seconds,
                        recall: 0.9 - index * 0.03,
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
    const graph = page.getByRole("img", { name: /RWKV Recall by Answer Time/ });
    await expect(graph).toBeVisible();
    await expect(graph.locator("circle")).toHaveCount(8);
    await expect(graph.locator("circle").first().locator("title")).toHaveText("2s: 90%");
    await expect(page.getByText("Answer time (seconds)")).toBeVisible();
    const overflow = await page.locator(".answer-time-recall").evaluate((container) => {
        const svg = container.querySelector("svg")!;
        return svg.getBoundingClientRect().right - container.getBoundingClientRect().right;
    });
    expect(overflow).toBeLessThanOrEqual(0.5);
    await page.locator(".answer-time-recall").screenshot({ path: testInfo.outputPath("answer-time-recall.png") });
    await page.evaluate(() => document.documentElement.classList.add("night-mode"));
    await page.locator(".answer-time-recall").screenshot({ path: testInfo.outputPath("answer-time-recall-dark.png") });

    await page.goto("/card-info/124");
    await expect(page.getByRole("img", { name: /RWKV Recall by Answer Time/ })).toHaveCount(0);
});
