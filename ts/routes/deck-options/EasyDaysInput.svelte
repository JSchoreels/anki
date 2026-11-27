<!--
Copyright: Anki Software, LLC and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script>
    import * as tr from "@generated/ftl";
    import Item from "$lib/components/Item.svelte";

    const easyDays = [
        tr.deckConfigEasyDaysMonday(),
        tr.deckConfigEasyDaysTuesday(),
        tr.deckConfigEasyDaysWednesday(),
        tr.deckConfigEasyDaysThursday(),
        tr.deckConfigEasyDaysFriday(),
        tr.deckConfigEasyDaysSaturday(),
        tr.deckConfigEasyDaysSunday(),
    ];

    export let values = [0, 0, 0, 0, 0, 0, 0];
    export let equalizer = false;
    const levels = [100, 75, 50, 25, 0];
    $: normalDays = values.filter((value) => value === 1).length;
    function updateDay(index, input) {
        const level = Number(input.value);
        if (values[index] !== 1 || normalDays > 1) {
            values[index] = level;
        }
        input.value = String(values[index]);
    }
</script>

<Item>
    {#if equalizer}
        <div class="equalizer" role="group" aria-label={tr.deckConfigStudyWeekLoad()}>
            <div class="scale" aria-hidden="true">
                {#each levels as level}<span>{level}%</span>{/each}
            </div>
            <div class="guides" aria-hidden="true">
                {#each levels as _}<i></i>{/each}
            </div>
            {#each easyDays as day, i}
                <label class="channel">
                    <input
                        type="range"
                        min="0"
                        max="1"
                        step=".25"
                        value={values[i]}
                        aria-label={day}
                        aria-orientation="vertical"
                        aria-valuetext={`${values[i] * 100}%`}
                        title={`${day} · ${values[i] * 100}%`}
                        on:input={(e) => updateDay(i, e.currentTarget)}
                    />
                    <span>{day}</span>
                </label>
            {/each}
        </div>
        <p class="week-caption">
            {tr.deckConfigStudyWeekLoad()}{#if normalDays === 1}
                · {tr.deckConfigStudyWeekGuard()}{/if}
        </p>
    {:else}
        <div class="container">
            <div class="easy-days-settings">
                <span></span>
                <span class="header min-col">{tr.deckConfigEasyDaysMinimum()}</span>
                <span class="header">{tr.deckConfigEasyDaysReduced()}</span>
                <span class="header normal-col">{tr.deckConfigEasyDaysNormal()}</span>

                {#each easyDays as day, index}
                    <span class="day">{day}</span>
                    <div class="input-container">
                        <input
                            type="range"
                            bind:value={values[index]}
                            step={0.25}
                            aria-label={day}
                            max={1.0}
                            min={0.0}
                            list="easy_day_steplist"
                        />
                    </div>
                {/each}
            </div>
        </div>
    {/if}
</Item>

<style lang="scss">
    .equalizer {
        position: relative;
        display: grid;
        grid-template-columns: 3.1rem repeat(7, minmax(0, 1fr));
        max-width: 38rem;
        margin-block: 1rem 0.4rem;
        font-size: 0.8rem;
    }
    .scale,
    .guides {
        display: flex;
        height: 5rem;
        flex-direction: column;
        justify-content: space-between;
    }
    .scale {
        color: var(--fg-subtle);
        align-items: flex-end;
        padding-inline-end: 0.65rem;
        font-size: 0.7rem;
    }
    .guides {
        position: absolute;
        inset-inline: 3.1rem 0;
        top: 0;
        padding-block: 0.35rem;
        pointer-events: none;
    }
    .guides i {
        border-top: 1px solid var(--border-subtle);
    }
    .channel {
        position: relative;
        display: grid;
        justify-items: center;
        gap: 0.35rem;
    }
    .channel input {
        writing-mode: vertical-lr;
        direction: rtl;
        width: 1.45rem;
        height: 5rem;
        margin: 0;
        accent-color: var(--border-focus);
        cursor: pointer;
    }
    .week-caption {
        margin: 0.35rem 0;
        color: var(--fg-subtle);
        font-size: 0.8rem;
    }
    .container {
        display: flex;
        justify-content: center;
    }
    .easy-days-settings {
        width: 100%;
        max-width: 1000px;
        border-collapse: collapse;

        display: grid;
        grid-template-columns: auto 1fr 1fr 1fr;

        border-collapse: collapse;
        & > * {
            padding: 8px 16px;
            border-bottom: var(--border) solid 1px;
        }
    }
    .input-container {
        grid-column: 2 / span 3;
    }
    span {
        display: flex;
        align-items: center;
        justify-content: center;

        &.min-col {
            justify-content: flex-start;
        }

        &.normal-col {
            justify-content: flex-end;
        }
    }
    .header {
        word-wrap: break-word;
        font-size: smaller;
    }
    .easy-days-settings input[type="range"] {
        width: 100%;
        cursor: pointer;
    }

    .day {
        word-wrap: break-word;
        font-size: smaller;
    }
</style>
