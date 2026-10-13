<!--
    Copyright: Ankitects Pty Ltd and contributors
    License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { renderMarkdown } from "@tslib/helpers";

    import type { HelpItem } from "./types";
    import { mdiEarth } from "./icons";
    import Icon from "./Icon.svelte";

    export let item: HelpItem;
</script>

<div class="help-section">
    <h2>
        {@html item.title}
        {#if item.global}
            <span
                class="icon"
                title={tr.deckConfigAffectsEntireCollection()}
                aria-hidden="true"
            >
                <Icon icon={mdiEarth} />
            </span>
        {/if}
    </h2>
    {#if item.help}
        {@html renderMarkdown(item.help)}
    {:else}
        {@html renderMarkdown(
            tr.helpNoExplanation({
                link: "[GitHub](https://github.com/ankitects/anki)",
            }),
        )}
    {/if}
</div>
{#if item.url || item.references?.length}
    <hr />
    <div class="chapter-redirect">
        {#if item.references?.length}
            <ul>
                {#each item.references as reference}
                    <li><a href={reference.url}>{reference.label}</a></li>
                {/each}
            </ul>
        {/if}
        {#if item.url}
            {@html renderMarkdown(
                tr.helpForMoreInfo({
                    link: `<a href="${item.url}" title="${tr.helpOpenManualChapter({
                        name: item.title,
                    })}">${item.title}</a>`,
                }),
            )}
        {/if}
    </div>
{/if}

<style lang="scss">
    h2 {
        margin-bottom: 1rem;
    }

    .help-section :global(h3) {
        font-size: 1.1rem;
        margin-top: 1.5rem;
        margin-bottom: 0.5rem;
    }

    .chapter-redirect {
        width: 100%;
        color: var(--fg-subtle);
        font-size: small;
    }

    .icon {
        display: inline-block;
        width: 1em;
        fill: currentColor;
        margin-inline-start: 0.25em;
        font-size: 1rem;
        vertical-align: middle;
    }

    li + li {
        margin-top: 0.5rem;
    }
</style>
