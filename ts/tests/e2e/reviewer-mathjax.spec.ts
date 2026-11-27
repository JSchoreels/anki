// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "./fixtures";

for (const preload of [true, false]) {
    test(`MathJax card transitions with preload=${preload}`, async ({ page }) => {
        const errors: string[] = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/reviewer-mathjax-test", (route) =>
            route.fulfill({
                contentType: "text/html",
                // Match the previewer's script order, or the reviewer's lazy loading.
                body: `<html><head>
                    <script>window.bridgeCommand = () => {};</script>
                    ${
                    preload
                        ? `<script src="/_anki/js/mathjax.js"></script>
                       <script src="/_anki/js/vendor/mathjax/tex-chtml-full.js"></script>`
                        : ""
                }
                    <script src="/_anki/js/reviewer.js"></script>
                    </head><body><div id="qa"></div></body></html>`,
            }));
        await page.goto("/reviewer-mathjax-test");

        await page.evaluate(() => {
            (window as any)._showQuestion("First \\(x^2\\)", "", "card");
        });
        await expect(page.locator("#qa mjx-container")).toBeVisible();
        await page.evaluate(() => {
            (window as any)._showAnswer("Answer \\(y^2\\)", "card");
        });
        await expect(page.locator("#qa")).toContainText("Answer");
        await expect(page.locator("#qa mjx-container")).toBeVisible();
        await page.evaluate(() => {
            (window as any)._showQuestion("Next card", "", "card");
        });
        await expect(page.locator("#qa")).toHaveText("Next card");
        await expect(page.locator("script[src=\"/_anki/js/mathjax.js\"]")).toHaveCount(1);
        await expect(page.locator("script[src=\"/_anki/js/vendor/mathjax/tex-chtml-full.js\"]")).toHaveCount(1);
        expect(errors).toEqual([]);
    });
}
