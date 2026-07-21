// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const PAINT_FALLBACK_MS = 100;

/** Wait until Chromium has had an opportunity to present the latest DOM. */
export function waitForNextPaint(): Promise<void> {
    return new Promise((resolve) => {
        let finished = false;
        const finish = (): void => {
            if (finished) {
                return;
            }
            finished = true;
            window.clearTimeout(fallback);
            resolve();
        };
        const fallback = window.setTimeout(finish, PAINT_FALLBACK_MS);

        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(finish);
        });
    });
}
