// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const DAY_SECS = 86_400;
const MINUTE_SECS = 60;
const HOUR_SECS = 60 * MINUTE_SECS;
const WEEK_SECS = 7 * DAY_SECS;
const MONTH_SECS = 30 * DAY_SECS;
const YEAR_SECS = 365 * DAY_SECS;
const MAX_INTERVAL_SECS = 36_500 * DAY_SECS;
const MAX_INTERVAL_DAYS = 36_500;

const MINIMUM_INTERVAL_UNITS: [string, number][] = [
    ["d", DAY_SECS],
    ["h", HOUR_SECS],
    ["m", MINUTE_SECS],
    ["s", 1],
];

const MAXIMUM_INTERVAL_UNITS: [string, number][] = [
    ["y", YEAR_SECS],
    ["m", MONTH_SECS],
    ["w", WEEK_SECS],
    ["d", DAY_SECS],
    ["s", 1],
];

export function minimumIntervalToString(seconds: number): string {
    const rounded = Math.max(1, Math.round(seconds));
    const [suffix, unitSeconds] = MINIMUM_INTERVAL_UNITS.find(([, unitSeconds]) => rounded % unitSeconds === 0)
        ?? MINIMUM_INTERVAL_UNITS[MINIMUM_INTERVAL_UNITS.length - 1];

    return `${rounded / unitSeconds}${suffix}`;
}

export function stringToMinimumInterval(text: string): number | undefined {
    const match = text.trim().match(/^([1-9]\d*)([smhd])$/i);
    if (!match) {
        return undefined;
    }

    const amount = Number(match[1]);
    const unit = match[2].toLowerCase();
    const unitSeconds = MINIMUM_INTERVAL_UNITS.find(([suffix]) => suffix === unit)?.[1];
    if (!unitSeconds) {
        return undefined;
    }

    return Math.min(amount * unitSeconds, MAX_INTERVAL_SECS);
}

export function maximumIntervalToString(days: number): string {
    const seconds = Math.max(1, Math.round(days)) * DAY_SECS;
    const [suffix, unitSeconds] = MAXIMUM_INTERVAL_UNITS.find(([, unitSeconds]) => seconds % unitSeconds === 0)
        ?? MAXIMUM_INTERVAL_UNITS.find(([suffix]) => suffix === "d")!;

    return `${seconds / unitSeconds}${suffix}`;
}

export function stringToMaximumInterval(text: string): number | undefined {
    const match = text.trim().match(/^([1-9]\d*)([sdwmy])$/i);
    if (!match) {
        return undefined;
    }

    const amount = Number(match[1]);
    const unit = match[2].toLowerCase();
    const unitSeconds = MAXIMUM_INTERVAL_UNITS.find(([suffix]) => suffix === unit)?.[1];
    if (!unitSeconds) {
        return undefined;
    }

    const seconds = Math.min(amount * unitSeconds, MAX_INTERVAL_SECS);
    return Math.min(Math.max(1, Math.ceil(seconds / DAY_SECS)), MAX_INTERVAL_DAYS);
}
