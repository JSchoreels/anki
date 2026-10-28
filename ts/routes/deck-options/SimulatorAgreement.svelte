<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { createLocaleNumberFormat } from "@tslib/i18n";
    import {
        agreementCells,
        type Agreement,
        type AgreementRun,
    } from "./simulator-agreement";

    export let runs: AgreementRun[];
    export let minimumRetention: number;

    const percent = createLocaleNumberFormat({
        style: "percent",
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
    });

    $: rows = runs.map((run) => ({
        ...run,
        cells: new Map(agreementCells(run).map((cell) => [cell.retention, cell])),
    }));
    $: retentions = [...new Set(rows.flatMap((row) => [...row.cells.keys()]))]
        .filter((retention) => retention >= minimumRetention)
        .sort((a, b) => a - b);

    function formatChange(value: number | undefined): string {
        if (value === undefined) {
            return "—";
        }
        if (Math.abs(value) < 0.0005) {
            return percent.format(0);
        }
        return `${value > 0 ? "+" : ""}${percent.format(value)}`;
    }

    function label(agreement: Agreement): string {
        switch (agreement) {
            case "fsrs":
                return tr.deckConfigRwkvComparisonAgreementFsrs();
            case "rwkv":
                return tr.deckConfigRwkvComparisonAgreementRwkv();
            case "mixed":
                return tr.deckConfigRwkvComparisonAgreementMixed();
            case "unavailable":
                return tr.deckConfigRwkvComparisonAgreementUnavailable();
        }
    }
</script>

<section class="agreement" aria-label={tr.deckConfigRwkvComparisonAgreementTitle()}>
    <h5>{tr.deckConfigRwkvComparisonAgreementTitle()}</h5>
    <p class="text-muted">{tr.deckConfigRwkvComparisonAgreementHelp()}</p>
    {#if retentions.length}
        <p class="text-muted">{tr.deckConfigRwkvComparisonAgreementMemoryOrder()}</p>
        <div class="agreement-scroll">
            <table>
                <thead>
                    <tr>
                        <th scope="col">
                            {tr.deckConfigRwkvComparisonAgreementRetention()}
                        </th>
                        {#each retentions as retention}
                            <th scope="col">{retention}%</th>
                        {/each}
                    </tr>
                </thead>
                <tbody>
                    {#each rows as row}
                        <tr>
                            <th scope="row">
                                {tr.deckConfigRwkvComparisonAgreementRun({
                                    run: String(row.run),
                                    preset: row.preset,
                                    days: row.days,
                                })}
                            </th>
                            {#each retentions as retention}
                                {@const cell = row.cells.get(retention)}
                                <td class={cell?.agreement ?? "unavailable"}>
                                    {#if cell}
                                        <strong>{label(cell.agreement)}</strong>
                                        <div>
                                            {tr.deckConfigRwkvComparisonAgreementFsrsAnswers()}
                                        </div>
                                        <div>
                                            {formatChange(cell.changes[0][0])} / {formatChange(
                                                cell.changes[0][1],
                                            )}
                                        </div>
                                        <div>
                                            {tr.deckConfigRwkvComparisonAgreementRwkvAnswers()}
                                        </div>
                                        <div>
                                            {formatChange(cell.changes[1][0])} / {formatChange(
                                                cell.changes[1][1],
                                            )}
                                        </div>
                                    {:else}
                                        —
                                    {/if}
                                </td>
                            {/each}
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    {:else}
        <p>{tr.deckConfigRwkvComparisonAgreementEmpty()}</p>
    {/if}
</section>

<style>
    .agreement {
        margin: 1.5rem 0;
    }
    .agreement-scroll {
        overflow-x: auto;
    }
    table {
        border-collapse: collapse;
        font-size: 0.85rem;
    }
    th,
    td {
        border: 1px solid var(--border);
        padding: 0.5rem;
        vertical-align: top;
    }
    th {
        min-width: 9rem;
    }
    td {
        min-width: 10rem;
    }
    td.rwkv {
        background: rgba(230, 126, 34, 0.2);
    }
    td.fsrs {
        background: rgba(52, 152, 219, 0.2);
    }
    td.mixed {
        background: rgba(128, 128, 128, 0.15);
    }
    td.unavailable {
        opacity: 0.7;
    }
</style>
