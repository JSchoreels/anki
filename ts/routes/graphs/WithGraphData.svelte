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

    function graphData(search: string, days: number): Promise<GraphsResponse> {
        const key = `${days}\0${search}`;
        if (inFlightGraphs && inFlightKey === key) {
            return inFlightGraphs;
        }

        inFlightKey = key;
        inFlightGraphs = graphs({ search, days }).finally(() => {
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
        loading = true;
        try {
            const data = await graphData(search, days);
            if (requestId === activeRequestId) {
                sourceData = data;
            }
        } finally {
            if (requestId === activeRequestId) {
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
