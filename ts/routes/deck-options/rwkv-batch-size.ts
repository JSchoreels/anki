// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export type RwkvBatchSizeOption = {
    size: number;
    estimatedMemoryMb: number;
    recommended?: boolean;
};

export const RWKV_REVIEW_BATCH_SIZE_RECOMMENDED = 512;

export const RWKV_REVIEW_BATCH_SIZE_OPTIONS: RwkvBatchSizeOption[] = [
    { size: 64, estimatedMemoryMb: 17 },
    { size: 128, estimatedMemoryMb: 34 },
    { size: 256, estimatedMemoryMb: 68 },
    { size: RWKV_REVIEW_BATCH_SIZE_RECOMMENDED, estimatedMemoryMb: 136, recommended: true },
    { size: 1024, estimatedMemoryMb: 272 },
    { size: 2048, estimatedMemoryMb: 545 },
];

export function rwkvBatchSizeSliderIndex(value: number): number {
    if (!Number.isFinite(value) || value <= 0) {
        return rwkvBatchSizeSliderIndex(RWKV_REVIEW_BATCH_SIZE_RECOMMENDED);
    }

    let closestIndex = 0;
    let closestDistance = Number.POSITIVE_INFINITY;
    for (const [index, option] of RWKV_REVIEW_BATCH_SIZE_OPTIONS.entries()) {
        const distance = Math.abs(option.size - value);
        if (distance < closestDistance) {
            closestIndex = index;
            closestDistance = distance;
        }
    }

    return closestIndex;
}

export function rwkvBatchSizeForSliderIndex(index: number): number {
    const clampedIndex = Math.max(
        0,
        Math.min(RWKV_REVIEW_BATCH_SIZE_OPTIONS.length - 1, Math.round(index)),
    );
    return RWKV_REVIEW_BATCH_SIZE_OPTIONS[clampedIndex].size;
}

export function rwkvBatchSizeOption(value: number): RwkvBatchSizeOption {
    return RWKV_REVIEW_BATCH_SIZE_OPTIONS[rwkvBatchSizeSliderIndex(value)];
}

export function rwkvEstimatedMemoryLabel(value: number): string {
    return `${rwkvBatchSizeOption(value).estimatedMemoryMb} MB`;
}
