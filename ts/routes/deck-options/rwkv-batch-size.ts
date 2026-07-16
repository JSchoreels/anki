// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export const RWKV_REVIEW_BATCH_SIZE_MIN = 64;
export const RWKV_REVIEW_BATCH_SIZE_MAX = 8192;
export const RWKV_REVIEW_BATCH_SIZE_RECOMMENDED = 512;

const RWKV_REVIEW_BATCH_STATE_MEMORY_MB_PER_CARD = 0.266;

export function rwkvEstimatedMemoryLabel(value: number): string {
    const validValue = Number.isFinite(value)
        ? Math.min(
            RWKV_REVIEW_BATCH_SIZE_MAX,
            Math.max(RWKV_REVIEW_BATCH_SIZE_MIN, Math.round(value)),
        )
        : RWKV_REVIEW_BATCH_SIZE_RECOMMENDED;
    return `${Math.round(validValue * RWKV_REVIEW_BATCH_STATE_MEMORY_MB_PER_CARD)} MB`;
}
