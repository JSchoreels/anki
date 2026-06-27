<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { GraphsResponse } from "@generated/anki/stats_pb";
    import {
        getGraphPreferences,
        graphs,
        setGraphPreferences,
    } from "@generated/backend";
    import { tick } from "svelte";
    import type { Writable } from "svelte/store";

    import { autoSavingPrefs } from "$lib/sveltelib/preferences";

    import { daysToRevlogRange } from "./graph-helpers";

    export let search: Writable<string>;
    export let days: Writable<number>;

    const prefsPromise = autoSavingPrefs(
        () => getGraphPreferences({}),
        setGraphPreferences,
    );

    let sourceData: GraphsResponse | null = null;
    let loading = true;
    let activeRequestId = 0;
    let inFlightKey = "";
    let inFlightGraphs: Promise<GraphsResponse> | null = null;
    let pendingSearch = $search;
    let pendingDays = $days;
    let updateScheduled = false;
    $: scheduleSourceDataUpdate($search, $days);

    function graphDebugLoggingEnabled(): boolean {
        return (
            typeof location !== "undefined" &&
            new URLSearchParams(location.search).has("graphDebug")
        );
    }

    function formatGraphDetails(details: Record<string, unknown>): string {
        return JSON.stringify(details);
    }

    function logGraphTiming(message: string, details: Record<string, unknown>): void {
        const text = `${message}: ${formatGraphDetails(details)}`;
        if (graphDebugLoggingEnabled()) {
            console.warn(text);
        } else {
            console.debug(text);
        }
    }

    function graphData(search: string, days: number): Promise<GraphsResponse> {
        const key = `${days}\0${search}`;
        if (inFlightGraphs && inFlightKey === key) {
            return inFlightGraphs;
        }

        inFlightKey = key;
        const start = performance.now();
        logGraphTiming("graphs request started", { search, days });
        inFlightGraphs = graphs({ search, days }).finally(() => {
            logGraphTiming("graphs request finished", {
                search,
                days,
                elapsedMs: performance.now() - start,
            });
            if (inFlightKey === key) {
                inFlightGraphs = null;
            }
        });
        return inFlightGraphs;
    }

    function scheduleSourceDataUpdate(search: string, days: number): void {
        pendingSearch = search;
        pendingDays = days;
        activeRequestId += 1;
        if (updateScheduled) {
            return;
        }

        updateScheduled = true;
        Promise.resolve().then(() => {
            updateScheduled = false;
            updateSourceData(pendingSearch, pendingDays, activeRequestId);
        });
    }

    async function updateSourceData(
        search: string,
        days: number,
        requestId: number,
    ): Promise<void> {
        // ensure the fast-loading preferences come first
        await prefsPromise;
        if (requestId !== activeRequestId) {
            return;
        }
        const start = performance.now();
        let applied = false;
        let slowTimer: number | undefined;
        loading = true;
        if (graphDebugLoggingEnabled()) {
            const slowTimerDetails = (): Record<string, unknown> => ({
                search,
                days,
                requestId,
                activeRequestId,
                elapsedMs: performance.now() - start,
            });
            slowTimer = window.setTimeout(() => {
                console.warn(
                    `graphs frontend still loading: ${formatGraphDetails(
                        slowTimerDetails(),
                    )}`,
                );
            }, 2000);
        }
        try {
            const data = await graphData(search, days);
            logGraphTiming("graphs data received", {
                search,
                days,
                requestId,
                activeRequestId,
                elapsedMs: performance.now() - start,
            });
            if (requestId === activeRequestId) {
                const applyStart = performance.now();
                sourceData = data;
                loading = false;
                await tick();
                applied = true;
                logGraphTiming("graphs data applied", {
                    search,
                    days,
                    requestId,
                    requestElapsedMs: applyStart - start,
                    applyElapsedMs: performance.now() - applyStart,
                    elapsedMs: performance.now() - start,
                });
            } else {
                logGraphTiming("graphs data ignored", {
                    search,
                    days,
                    requestId,
                    activeRequestId,
                    elapsedMs: performance.now() - start,
                });
            }
        } finally {
            if (slowTimer != null) {
                window.clearTimeout(slowTimer);
            }
            if (!applied && requestId === activeRequestId) {
                loading = false;
            }
        }
    }

    $: revlogRange = daysToRevlogRange($days);
</script>

<!--
We block graphs loading until the preferences have been fetched, so graphs
don't have to worry about a null initial value. We don't do the same for the
graph data, as it gets updated as the user changes options, and we don't want
the current graphs to disappear until the new graphs have finished loading.
-->
{#await prefsPromise then prefs}
    <slot {revlogRange} {prefs} {sourceData} {loading} />
{/await}
