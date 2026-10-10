<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    export let title: string;
</script>

<button
    type="button"
    class="scheduler-help"
    aria-label={`${tr.actionsHelp()}: ${title}`}
    on:click
>
    <span aria-hidden="true">?</span>
</button>

<style>
    button.scheduler-help {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.5rem;
        height: 1.5rem;
        padding: 0;
        margin: 0;
        border-radius: 50%;
        color: var(--fg-subtle);
        cursor: help;
        flex-shrink: 0;
    }

    button.scheduler-help,
    button.scheduler-help:hover,
    button.scheduler-help:active {
        border: 0;
        background: transparent;
        box-shadow: none;
    }

    button.scheduler-help:focus-visible {
        /* Native webview styles suppress button outlines. */
        outline: 2px solid var(--border-focus) !important;
        outline-offset: 2px;
    }

    span {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1rem;
        height: 1rem;
        border: 1px solid var(--border);
        border-radius: 50%;
        font-size: 0.7rem;
    }

    button.scheduler-help:hover {
        color: var(--fg);
    }

    @media (pointer: coarse) {
        button.scheduler-help {
            width: 44px;
            height: 44px;
        }
    }
</style>
