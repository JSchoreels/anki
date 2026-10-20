// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { CardId } from "@generated/anki/cards_pb";
import { CardStatsResponse } from "@generated/anki/stats_pb";

import { expect, test } from "./fixtures";
import { decodeRequestBody } from "./helpers";

for (const path of ["/card-info/123", "/card-info/123/456"]) {
    test(`RWKV diagnostics recover without reopening ${path}`, async ({ page }) => {
        const requests = new Map<bigint, number>();
        await page.route("**/_anki/cardStats", async (route) => {
            const { cid } = decodeRequestBody(route.request(), CardId);
            const count = (requests.get(cid) ?? 0) + 1;
            requests.set(cid, count);
            const pending = cid === 123n && count < 3;
            await route.fulfill({
                contentType: "application/binary",
                body: Buffer.from(new CardStatsResponse({
                    cardId: cid,
                    added: 1700000000n,
                    rwkvRetryPending: pending,
                    extraRows: [{
                        label: "RWKV computed R",
                        value: pending ? "Unavailable" : "64%",
                    }],
                }).toBinary()),
            });
        });

        await page.goto(path);
        await expect(page.getByText("Unavailable", { exact: true })).toBeVisible();
        await expect(page.getByText("Unavailable", { exact: true })).toHaveCount(0);
        await expect(page.getByText("64%", { exact: true })).toHaveCount(path.endsWith("456") ? 2 : 1);
        expect(requests.get(123n)).toBe(3);
    });
}
