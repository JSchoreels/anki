<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import CheckBox from "$lib/components/CheckBox.svelte";
    import Modal from "$lib/components/Modal.svelte";

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

<div class="m-1">
    <button type="button" class="btn btn-primary" on:click={show}>
        {tr.deckConfigApplyScheduleToAllPresets()}
    </button>
</div>

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
        <div class="copy-option">
            <CheckBox bind:value={options.retention}>
                {tr.deckConfigCopyRetention()}
            </CheckBox>
            <p>{tr.deckConfigCopyRetentionDescription()}</p>
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
    .copy-option p {
        color: var(--fg-subtle);
        font-size: 0.85rem;
        margin: 0.25rem 0 0.75rem;
    }
</style>
