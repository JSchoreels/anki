<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import CheckBox from "$lib/components/CheckBox.svelte";
    import Modal from "$lib/components/Modal.svelte";
    import Icon from "$lib/components/Icon.svelte";
    import { mdiCopy } from "$lib/components/icons";

    import { defaultScheduleCopyOptions } from "./apply-schedule";
    import { commitEditing, type DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;

    let modal: Modal;
    let options = defaultScheduleCopyOptions();

    async function show(): Promise<void> {
        await commitEditing();
        options = defaultScheduleCopyOptions();
        modal.show();
    }
</script>

<button type="button" class="apply-schedule" on:click={show}>
    <span class="copy-icon" aria-hidden="true"><Icon icon={mdiCopy} /></span>
    {tr.deckConfigApplyScheduleToAllPresets()}
</button>

<Modal
    bind:this={modal}
    labelId="apply-schedule-title"
    cancelOnEscape
    onOkClicked={() => state.applyScheduleToAllPresets(options)}
>
    <div slot="header" class="modal-header">
        <h5 class="modal-title" id="apply-schedule-title">
            {tr.deckConfigApplyScheduleToAllPresets()}
        </h5>
    </div>
    <div slot="body" class="modal-body">
        <p>{tr.deckConfigApplyScheduleDescription()}</p>
        <div class="copy-option">
            <CheckBox bind:value={options.scheduler}>
                {tr.deckConfigCopyScheduler()}
            </CheckBox>
            <p>{tr.deckConfigCopySchedulerDescription()}</p>
        </div>
        <div class="copy-option">
            <CheckBox bind:value={options.retention}>
                {tr.deckConfigCopyRetention()}
            </CheckBox>
            <p>{tr.deckConfigCopyRetentionDescription()}</p>
        </div>
        <div class="copy-option">
            <CheckBox bind:value={options.fallback}>
                {tr.deckConfigCopyFallback()}
            </CheckBox>
            <p>{tr.deckConfigCopyFallbackDescription()}</p>
        </div>
        <div class="copy-option">
            <CheckBox bind:value={options.settings}>
                {tr.deckConfigCopySettings()}
            </CheckBox>
            <p>{tr.deckConfigCopySettingsDescription()}</p>
        </div>
    </div>
    <div slot="footer" class="modal-footer">
        <button type="button" class="btn btn-secondary" on:click={modal.cancelHandler}>
            {tr.actionsCancel()}
        </button>
        <button
            type="button"
            class="btn btn-primary"
            disabled={!Object.values(options).some(Boolean)}
            on:click={modal.acceptHandler}
        >
            {tr.deckConfigApplySchedule()}
        </button>
    </div>
</Modal>

<style>
    .apply-schedule {
        display: inline-flex;
        align-items: center;
        gap: 0.375rem;
        padding: 0.25rem 0;
        min-height: 2rem;
        border: 0;
        background: transparent;
        color: var(--border-focus);
        font-size: 0.75rem;
        cursor: pointer;
        text-align: start;
    }

    .apply-schedule:hover {
        text-decoration: underline;
    }

    .copy-icon {
        display: inline-flex;
        width: 0.875rem;
        height: 0.875rem;
        fill: currentColor;
        flex-shrink: 0;
    }

    @media (pointer: coarse) {
        .apply-schedule {
            min-height: 44px;
        }
    }

    .copy-option p {
        color: var(--fg-subtle);
        font-size: 0.85rem;
        margin: 0.25rem 0 0.75rem;
    }
</style>
