<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { GraphsRequest, GraphsResponse } from "@generated/anki/stats_pb";
    import { getGraphPreferences, setGraphPreferences } from "@generated/backend";
    import { postProtoWithResponse } from "@generated/post";
    import { onDestroy, tick } from "svelte";
    import type { Writable } from "svelte/store";

    import { autoSavingPrefs } from "$lib/sveltelib/preferences";

    import { daysToRevlogRange } from "./graph-helpers";

    export let search: Writable<string>;
    export let days: Writable<number>;

    const prefsPromise = autoSavingPrefs(
        () => getGraphPreferences({}),
        setGraphPreferences,
    );
    const rwkvStatsPendingHeader = "X-Anki-Rwkv-Stats-Pending";
    const rwkvStatsRetryDelayMs = 2_000;
    const rwkvStatsMaxRetries = 3;

    let sourceData: GraphsResponse | null = null;
    let loading = true;
    let activeRequestId = 0;
    let inFlightKey = "";
    let inFlightGraphs: Promise<GraphDataResponse> | null = null;
    let currentSearch = $search;
    let currentDays = $days;
    let pendingSearch = $search;
    let pendingDays = $days;
    let updateScheduled = false;
    let rwkvStatsRetryKey = "";
    let rwkvStatsRetryCount = 0;
    let rwkvStatsRetryTimer: number | undefined;
    $: currentSearch = $search;
    $: currentDays = $days;
    $: scheduleSourceDataUpdate($search, $days);

    interface GraphDataResponse {
        data: GraphsResponse;
        rwkvStatsPending: boolean;
    }

    function graphDataKey(search: string, days: number): string {
        return `${days}\0${search}`;
    }

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

    function graphData(search: string, days: number): Promise<GraphDataResponse> {
        const key = graphDataKey(search, days);
        if (inFlightGraphs && inFlightKey === key) {
            return inFlightGraphs;
        }

        inFlightKey = key;
        const start = performance.now();
        logGraphTiming("graphs request started", { search, days });
        inFlightGraphs = postProtoWithResponse(
            "graphs",
            new GraphsRequest({ search, days }),
            GraphsResponse,
        )
            .then(({ output, headers }) => ({
                data: output,
                rwkvStatsPending: headers.get(rwkvStatsPendingHeader) === "1",
            }))
            .finally(() => {
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

    function clearRwkvStatsRetryTimer(): void {
        if (rwkvStatsRetryTimer != null) {
            window.clearTimeout(rwkvStatsRetryTimer);
            rwkvStatsRetryTimer = undefined;
        }
    }

    function resetRwkvStatsRetryForKey(key: string): void {
        if (rwkvStatsRetryKey !== key) {
            clearRwkvStatsRetryTimer();
            rwkvStatsRetryKey = key;
            rwkvStatsRetryCount = 0;
        }
    }

    function handleRwkvStatsRetry(
        search: string,
        days: number,
        requestId: number,
        rwkvStatsPending: boolean,
    ): void {
        const key = graphDataKey(search, days);
        resetRwkvStatsRetryForKey(key);
        if (!rwkvStatsPending) {
            clearRwkvStatsRetryTimer();
            rwkvStatsRetryCount = 0;
            return;
        }
        if (rwkvStatsRetryTimer != null) {
            return;
        }
        if (rwkvStatsRetryCount >= rwkvStatsMaxRetries) {
            logGraphTiming("graphs RWKV stats retry exhausted", {
                search,
                days,
                requestId,
                retries: rwkvStatsRetryCount,
            });
            return;
        }

        rwkvStatsRetryCount += 1;
        const retry = rwkvStatsRetryCount;
        logGraphTiming("graphs RWKV stats retry scheduled", {
            search,
            days,
            requestId,
            retry,
            delayMs: rwkvStatsRetryDelayMs,
        });
        rwkvStatsRetryTimer = window.setTimeout(() => {
            rwkvStatsRetryTimer = undefined;
            if (
                requestId !== activeRequestId ||
                search !== currentSearch ||
                days !== currentDays
            ) {
                logGraphTiming("graphs RWKV stats retry ignored", {
                    search,
                    days,
                    requestId,
                    activeRequestId,
                });
                return;
            }
            logGraphTiming("graphs RWKV stats retry started", {
                search,
                days,
                requestId,
                retry,
            });
            scheduleSourceDataUpdate(search, days);
        }, rwkvStatsRetryDelayMs);
    }

    function scheduleSourceDataUpdate(search: string, days: number): void {
        pendingSearch = search;
        pendingDays = days;
        resetRwkvStatsRetryForKey(graphDataKey(search, days));
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
                rwkvStatsPending: data.rwkvStatsPending,
            });
            if (requestId === activeRequestId) {
                const applyStart = performance.now();
                sourceData = data.data;
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
                    rwkvStatsPending: data.rwkvStatsPending,
                });
                handleRwkvStatsRetry(search, days, requestId, data.rwkvStatsPending);
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

    onDestroy(clearRwkvStatsRetryTimer);
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
