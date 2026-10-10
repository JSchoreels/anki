<!--
    Copyright: Ankitects Pty Ltd and contributors
    License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    /* This component accepts an array of tabs and a value. Whenever a tab is
    activated, its last used value is applied to its provided setter and the
    component's value. Whenever it's deactivated, its setter is called with its
    disabledValue. */
    import type { ValueTab } from "./lib";

    export let tabs: ValueTab[];
    export let value: number;
    export let compact = false;

    let activeTab = lastSetTab();
    $: onTabChanged(activeTab);
    $: value = tabs[activeTab].value ?? 0;
    $: tabs[activeTab].setValue(value);

    function lastSetTab(): number {
        const revIdx = tabs
            .slice()
            .reverse()
            .findIndex((tab) => tab.value !== null);
        return revIdx === -1 ? 0 : tabs.length - revIdx - 1;
    }

    function onTabChanged(newTab: number) {
        for (const [idx, tab] of tabs.entries()) {
            if (newTab === idx) {
                tab.enable(value);
            } else if (newTab > idx) {
                /* antecedent tabs are obscured, so we can preserve their original values */
                tab.reset();
            } else {
                /* but subsequent tabs would obscure, so they must be nulled */
                tab.disable();
            }
        }
    }

    const handleClick = (tabValue: number) => () => (activeTab = tabValue);
</script>

<ul class:compact>
    {#each tabs as tab, idx}
        <li class:active={activeTab === idx}>
            <button aria-pressed={activeTab === idx} on:click={handleClick(idx)}>
                {tab.title}
            </button>
        </li>
    {/each}
</ul>

<style lang="scss">
    ul {
        width: 100%;
        display: flex;
        flex-wrap: nowrap;
        &:has(li:nth-child(3)) {
            justify-content: space-between;
        }
        justify-content: space-around;
        padding-inline: 0;
        margin-bottom: 0.5rem;
        list-style: none;
    }

    button {
        display: block;
        white-space: nowrap;
        cursor: pointer;
        color: var(--fg-subtle);
        border: 1px solid transparent;
        background-color: transparent;
        /* remove default macOS styling */
        box-shadow: none;
        font-size: smaller;
    }

    li.active > button {
        color: var(--fg);
        border-bottom: 4px solid var(--border-focus);
        border-radius: 0;
    }
    button:hover {
        color: var(--fg);
    }

    ul.compact {
        width: auto;
        margin: 0;
        padding: 0.125rem;
        background: var(--canvas-inset);
        border-radius: 0.5rem;
    }

    .compact button {
        min-height: 1.75rem;
        padding: 0.1875rem 0.75rem;
        border: 1px solid transparent;
        border-radius: 0.375rem;
        font-size: 0.75rem;
    }

    .compact li.active > button {
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: 0.375rem;
    }

    @media (pointer: coarse) {
        .compact button {
            min-height: 44px;
        }
    }
</style>
