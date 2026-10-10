<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    export let title: string;
</script>

<button type="button" aria-label={`${tr.actionsHelp()}: ${title}`} on:click>
    <span aria-hidden="true">?</span>
</button>

<style>
    button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.5rem;
        height: 1.5rem;
        padding: 0;
        border: 0;
        background: transparent;
        color: var(--fg-subtle);
        cursor: help;
        flex-shrink: 0;
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

    button:hover {
        color: var(--fg);
    }

    @media (pointer: coarse) {
        button {
            width: 44px;
            height: 44px;
        }
    }
</style>
