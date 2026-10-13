<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { pageTheme } from "$lib/sveltelib/theme";

    const rtl: boolean = window.getComputedStyle(document.body).direction == "rtl";

    export let id: string | undefined = undefined;
    let className: string = "";
    export { className as class };

    export let title: string;
</script>

<div
    {id}
    class="container {className}"
    class:light={!$pageTheme.isDark}
    class:dark={$pageTheme.isDark}
    class:rtl
    style:--gutter-block="2px"
    style:--container-margin="0"
>
    <div class="position-relative" class:inline-heading={!!$$slots["header-actions"]}>
        <h1>
            {title}
        </h1>
        <slot name="header-actions" />
        <div class="help-badge position-absolute" class:rtl>
            <slot name="tooltip" />
        </div>
    </div>
    <slot />
</div>

<style lang="scss">
    @use "../sass/elevation" as *;
    .container {
        width: 100%;
        background: var(--canvas-elevated);
        border: 1px solid var(--border-subtle);
        border-radius: var(--border-radius-medium, 10px);

        &.light {
            @include elevation(3);
        }
        &.dark {
            @include elevation(4);
        }

        padding: 1rem 1.75rem 0.75rem 1.25rem;
        &.rtl {
            padding: 1rem 1.25rem 0.75rem 1.75rem;
        }
        page-break-inside: avoid;
    }
    h1 {
        border-bottom: 1px solid var(--border);
        padding-bottom: 0.25em;
    }
    .help-badge {
        right: 0;
        top: 0;
        color: #555;
        &.rtl {
            right: unset;
            left: 0;
        }
    }

    .inline-heading {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 0.5rem 1rem;
        margin-bottom: 1rem;

        h1 {
            border: 0;
            padding: 0;
            margin: 0;
            font-size: 1.5rem;
        }
    }

    :global(.night-mode) .help-badge {
        color: var(--fg);
    }
</style>
