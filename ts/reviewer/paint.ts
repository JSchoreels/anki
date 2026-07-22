// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const PAINT_STALL_MS = 100;
const PAINT_RETRY_MS = 500;

/** Wait until Chromium has had an opportunity to present the latest DOM. */
export function waitForNextPaint(
    onStalled: () => void,
    onRetry: () => void,
): Promise<void> {
    return new Promise((resolve) => {
        let finished = false;
        const finish = (): void => {
            if (finished) {
                return;
            }
            finished = true;
            window.clearTimeout(stallTimer);
            window.clearInterval(retryTimer);
            resolve();
        };
        const stallTimer = window.setTimeout(() => {
            if (!finished) {
                onStalled();
            }
        }, PAINT_STALL_MS);
        const retryTimer = window.setInterval(() => {
            if (!finished) {
                onRetry();
            }
        }, PAINT_RETRY_MS);

        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(finish);
        });
    });
}
