// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * Suite 1 – note-add roundtrip (issue #4930)
 *
 * Verifies that the new TypeScript editor (PR #4029) sends a well-formed
 * addNote RPC payload when the user types into fields and clicks Add.
 *
 * Assertions:
 *  - /_anki/addNote request body decodes to AddNoteRequest with correct field
 *    values and a non-zero deckId.
 *  - /_anki/updateNotes is NOT fired (add-mode contract: updating existing
 *    notes must never happen during an add flow).
 *  - /_anki/newNote fires after Add (signals the form was reset).
 *  - First field clears after Add.
 *  - window.__bridgeCalls contains "saved" (NoteEditor.svelte:438).
 *
 * This test mutates the collection — a note is persisted on every run.
 *
 * Suite 1b – empty field validation (issue #4930)
 *
 * Verifies that clicking Add with an empty first field does NOT fire the
 * addNote RPC, matching the legacy noteCanBeAdded() guard in editor_legacy.py.
 */

import { AddNoteRequest, AddNoteResponse } from "@generated/anki/notes_pb";
import type { BrowserContext, Page } from "@playwright/test";

import { expect, installBridgeStub, test } from "./fixtures";
import { bridgeCalls, decodeRequestBody, editableField, isRpc, rpcUrl } from "./helpers";

async function editCurrentNote(addPage: Page, context: BrowserContext, front: string): Promise<Page> {
    const field = editableField(addPage, 0);
    await field.click();
    await field.pressSequentially(front);

    const addRequestPromise = addPage.waitForRequest(isRpc("addNote"));
    const addResponsePromise = addPage.waitForResponse(
        (response) => isRpc("addNote")(response.request()),
    );
    await addPage.getByRole("button", { name: "Add", exact: true }).click();
    const addRequest = decodeRequestBody(await addRequestPromise, AddNoteRequest);
    const addResponse = AddNoteResponse.fromBinary(
        new Uint8Array(await (await addResponsePromise).body()),
    );

    const page = await context.newPage();
    await installBridgeStub(page);
    await page.goto("/editor/?mode=current", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
        () => typeof (window as any).loadNote === "function",
        { timeout: 15_000 },
    );
    await page.evaluate(
        ({ nid, notetypeId }) =>
            (window as any).loadNote({
                nid: BigInt(nid),
                notetypeId: BigInt(notetypeId),
                initial: true,
            }),
        {
            nid: addResponse.noteId.toString(),
            notetypeId: addRequest.note!.notetypeId.toString(),
        },
    );
    await expect(editableField(page, 0)).toHaveText(front);
    return page;
}

test("immediately clicking Add while second field is focused includes its latest value", async ({ editor: page }) => {
    const field0 = editableField(page, 0);
    const field1 = editableField(page, 1);

    await field0.click();
    await field0.pressSequentially("Committed Front");

    await field1.click();
    await field1.pressSequentially("Focused Back");

    const addNoteReqPromise = page.waitForRequest(isRpc("addNote"), { timeout: 10_000 });

    await page.getByRole("button", { name: "Add", exact: true }).click();

    const decoded = decodeRequestBody(await addNoteReqPromise, AddNoteRequest);

    expect(decoded.note?.fields[0]).toBe("Committed Front");
    expect(decoded.note?.fields[1]).toBe("Focused Back");
});

test("saveNow commits an active IME composition before reading the field", async ({ editor: page }) => {
    const field = editableField(page, 0);

    await field.click();
    await field.pressSequentially("諦[");
    await field.evaluate((element) => {
        window.dispatchEvent(new CompositionEvent("compositionstart"));
        element.addEventListener(
            "blur",
            () => {
                element.textContent = "諦[あきら]める";
                window.dispatchEvent(new CompositionEvent("compositionend"));
            },
            { once: true },
        );
    });

    const savedField = await page.evaluate(async () => {
        await (window as any).saveNow();
        return (window as any).getNoteInfo().fields[0];
    });

    expect(savedField).toBe("諦[あきら]める");
});

for (const mode of ["add", "current"] as const) {
    test(`IME text committed immediately before blur is saved in ${mode} mode`, async ({ editor: addPage, context }) => {
        const page = mode === "current" ? await editCurrentNote(addPage, context, "弁当[]") : addPage;
        const field = editableField(page, 0);
        await field.click();
        if (mode === "add") {
            await field.pressSequentially("弁当[]");
        }

        await field.evaluate((element) => {
            element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, composed: true }));
            element.textContent = "弁当[べんとう]";
            element.dispatchEvent(
                new CompositionEvent("compositionend", {
                    data: "べんとう",
                    bubbles: true,
                    composed: true,
                }),
            );
            // Focus may leave before the final DOM mutation reaches the field store.
            element.blur();
        });

        if (mode === "add") {
            const request = page.waitForRequest(isRpc("addNote"));
            await page.getByRole("button", { name: "Add", exact: true }).click();
            expect(decodeRequestBody(await request, AddNoteRequest).note?.fields[0]).toBe("弁当[べんとう]");
        } else {
            await page.evaluate(async () => {
                await (window as any).saveNow();
                await (window as any).reloadNote();
            });
            await expect(field).toHaveText("弁当[べんとう]");
        }
    });
}

test("saveNow waits for the blur save before reporting completion", async ({ editor: addPage, context }) => {
    const page = await editCurrentNote(addPage, context, "original");

    // Let the initial field-store synchronization finish before isolating the
    // save caused by focus loss.
    await page.waitForTimeout(700);

    let releaseBlurSave!: () => void;
    const blurSaveGate = new Promise<void>((resolve) => {
        releaseBlurSave = resolve;
    });
    let markBlurSaveStarted!: () => void;
    const blurSaveStarted = new Promise<void>((resolve) => {
        markBlurSaveStarted = resolve;
    });
    await page.route(
        "**/_anki/updateNotes",
        async (route) => {
            markBlurSaveStarted();
            await blurSaveGate;
            await route.continue();
        },
        { times: 1 },
    );

    await page.evaluate(() => ((window as any).__bridgeCalls = []));
    await editableField(page, 0).click();
    await editableField(page, 0).evaluate((element) => element.blur());
    const save = page.evaluate(() => (window as any).saveNow());

    await blurSaveStarted;
    expect(await bridgeCalls(page)).not.toContain("saved");

    releaseBlurSave();
    await save;
    expect(await bridgeCalls(page)).toContain("saved");
});

test("typing into fields and clicking Add sends correct addNote payload", async ({ editor: page }) => {
    const field0 = editableField(page, 0);
    const field1 = editableField(page, 1);

    // pressSequentially() fires real keydown/keypress/keyup events, which are
    // necessary for the Svelte content store to detect changes in the shadow-DOM
    // contenteditable (fill() only sets textContent and may miss the debounce).
    await field0.click();
    await field0.pressSequentially("Hello World");

    await field1.click();
    await field1.pressSequentially("Goodbye World");

    // Move focus away from field 1 so this test verifies the ordinary
    // committed-field roundtrip. A separate test below covers the focused-field
    // add race.
    await field0.click();

    // Track whether the forbidden updateNotes RPC fires at any point.
    let updateNotesFired = false;
    page.on("request", (req) => {
        if (isRpc("updateNotes")(req)) {
            updateNotesFired = true;
        }
    });

    // Set up addNote capture BEFORE clicking Add.
    // waitForRequest resolves on the next matching request, so it is safe to
    // set it up here without racing against earlier background RPCs.
    const addNoteReqPromise = page.waitForRequest(isRpc("addNote"), { timeout: 10_000 });

    // exact: true avoids matching the "Add tag" button in the tag editor.
    await page.getByRole("button", { name: "Add", exact: true }).click();

    const addNoteReq = await addNoteReqPromise;
    const decoded = decodeRequestBody(addNoteReq, AddNoteRequest);

    expect(decoded.note?.fields[0]).toBe("Hello World");
    expect(decoded.note?.fields[1]).toBe("Goodbye World");
    expect(decoded.deckId).not.toBe(0n);

    // Response must be successful.
    await page.waitForResponse(
        (resp) => resp.url().includes(rpcUrl("addNote")) && resp.status() < 400,
        { timeout: 10_000 },
    );

    // After a successful add, the editor calls loadNote({ stickyFieldsFrom:
    // note }) which in turn calls newNote. This is the reliable "form was
    // reset" signal (the 500 ms toast is too short to assert reliably).
    await page.waitForRequest(isRpc("newNote"), { timeout: 10_000 });

    // Both fields must clear after the add (addCurrentNoteInner calls
    // loadNote({ stickyFieldsFrom }) which resets non-sticky fields).
    await expect(field0).toHaveText("", { timeout: 5_000 });
    await expect(field1).toHaveText("", { timeout: 5_000 });

    // addCurrentNoteInner() calls saveNow() before addNote; saveNow sends
    // bridgeCommand("saved") when !isLegacy. The test environment loads
    // without Qt so isLegacy is always false here.
    const calls = await bridgeCalls(page);
    expect(calls).toContain("saved");

    // updateNotes must never fire during an add flow.
    expect(updateNotesFired).toBe(false);
});

test("clicking Add with empty fields does not fire addNote", async ({ editor: page }) => {
    // Fields are empty after loadNote({ initial: true }) — do not type anything.

    // Set up listener BEFORE clicking so no fire can be missed.
    // waitForRequest rejects with TimeoutError if no match arrives within the
    // timeout — that rejection IS the passing condition here.
    const addNotePromise = page.waitForRequest(isRpc("addNote"), { timeout: 2_000 });

    await page.getByRole("button", { name: "Add", exact: true }).click();

    // If addNote fires the promise resolves and rejects.toThrow() fails — which
    // is the correct failure signal. If it does not fire within 2 s the promise
    // rejects with TimeoutError and the assertion passes.
    await expect(addNotePromise).rejects.toThrow();
});
