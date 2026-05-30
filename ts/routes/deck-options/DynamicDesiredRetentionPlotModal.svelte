<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as d3 from "d3";
    import Modal from "bootstrap/js/dist/modal";
    import { createEventDispatcher } from "svelte";

    import {
        costWeightForAverageDr,
        evaluateDynamicDesiredRetention,
        targetDrCalibration,
        validCalibration,
        validPolicyParams,
        validRetentionBounds,
    } from "./dynamic-desired-retention";

    export let modal: Modal | null = null;
    export let inline = false;
    export let params: number[] = [];
    export let calibrationWeights: number[] = [];
    export let calibrationAvgDrs: number[] = [];
    export let fsrsEquivalentWeights: number[] = [];
    export let fsrsEquivalentDrs: number[] = [];
    export let retentionMin = 0.3;
    export let retentionMax = 0.995;
    export let targetAverageDr = 0.9;

    const dispatch = createEventDispatcher<{ saveTarget: number; close: void }>();
    let svgElement: SVGSVGElement;
    let yaw = -42;
    let pitch = 24;
    let lastX = 0;
    let lastY = 0;
    let draftTargetDr = targetAverageDr;
    let syncedTargetDr = targetAverageDr;
    let projectionScale = 190;

    $: if (targetAverageDr !== syncedTargetDr) {
        syncedTargetDr = targetAverageDr;
        draftTargetDr = targetAverageDr;
    }
    $: targetCalibration = targetDrCalibration(
        calibrationWeights,
        calibrationAvgDrs,
        fsrsEquivalentWeights,
        fsrsEquivalentDrs,
    );
    $: hasTargetCalibration = validCalibration(
        targetCalibration.weights,
        targetCalibration.drs,
    );
    $: selectorMin = hasTargetCalibration ? Math.min(...targetCalibration.drs) : 0;
    $: selectorMax = hasTargetCalibration ? Math.max(...targetCalibration.drs) : 1;
    $: inferredWeight = costWeightForAverageDr(
        draftTargetDr,
        targetCalibration.weights,
        targetCalibration.drs,
    );
    $: canPlot = canPlotWithWeight(inferredWeight);
    $: if (svgElement) {
        renderPlot();
    }

    function setupModal(node: HTMLDivElement) {
        modal = new Modal(node);
        const onHidden = () => dispatch("close");
        node.addEventListener("shown.bs.modal", renderPlot);
        node.addEventListener("hidden.bs.modal", onHidden);
        return {
            destroy() {
                node.removeEventListener("shown.bs.modal", renderPlot);
                node.removeEventListener("hidden.bs.modal", onHidden);
                modal?.dispose();
                modal = null;
            },
        };
    }

    function closeRequested(): void {
        if (inline) {
            dispatch("close");
        } else {
            modal?.hide();
        }
    }

    function renderPlot(): void {
        if (!svgElement) {
            return;
        }

        const currentWeight = costWeightForAverageDr(
            draftTargetDr,
            targetCalibration.weights,
            targetCalibration.drs,
        );
        const svg = d3.select(svgElement);
        svg.selectAll("*").remove();
        const width = svgElement.clientWidth || 860;
        const height = svgElement.clientHeight || 560;
        svg.attr("viewBox", `0 0 ${width} ${height}`);

        if (!canPlotWithWeight(currentWeight) || currentWeight === null) {
            svg.append("text")
                .attr("x", width / 2)
                .attr("y", height / 2)
                .attr("text-anchor", "middle")
                .attr("class", "plot-empty")
                .text("Dynamic DR policy and calibration are required.");
            return;
        }

        const stabilityCount = 34;
        const difficultyCount = 24;
        const logSMin = Math.log(0.1);
        const logSMax = Math.log(1000);
        const surface: SurfacePoint[][] = [];
        for (let y = 0; y < difficultyCount; y++) {
            const difficulty = 1 + (9 * y) / (difficultyCount - 1);
            const row: SurfacePoint[] = [];
            for (let x = 0; x < stabilityCount; x++) {
                const stability = Math.exp(
                    logSMin + ((logSMax - logSMin) * x) / (stabilityCount - 1),
                );
                const dr = evaluateDynamicDesiredRetention(
                    params,
                    stability,
                    difficulty,
                    currentWeight,
                    retentionMin,
                    retentionMax,
                );
                row.push({
                    x: (x / (stabilityCount - 1) - 0.5) * 2,
                    y: (y / (difficultyCount - 1) - 0.5) * 2,
                    z: retentionToAxis(dr),
                    stability,
                    difficulty,
                    dr,
                });
            }
            surface.push(row);
        }

        const color = d3
            .scaleSequential(d3.interpolateViridis)
            .domain([retentionMin, retentionMax]);
        const axes = axisDefinitions();
        const tickPoints = axisTickPoints();
        const plotHorizontalPadding = 24;
        const plotTopPadding = 70;
        const plotBottomPadding = 12;
        projectionScale = 1;
        const unitBounds = projectedBounds(surface, axes, tickPoints);
        projectionScale = Math.min(
            (width - plotHorizontalPadding * 2) /
                Math.max(1, unitBounds.maxX - unitBounds.minX),
            (height - plotTopPadding - plotBottomPadding) /
                Math.max(1, unitBounds.maxY - unitBounds.minY),
        );
        const projected = surface.map((row) => row.map(project));
        const cells: Cell[] = [];
        for (let y = 0; y < difficultyCount - 1; y++) {
            for (let x = 0; x < stabilityCount - 1; x++) {
                const points = [
                    projected[y][x],
                    projected[y][x + 1],
                    projected[y + 1][x + 1],
                    projected[y + 1][x],
                ];
                const source = surface[y][x];
                const corners = [
                    surface[y][x],
                    surface[y][x + 1],
                    surface[y + 1][x + 1],
                    surface[y + 1][x],
                ];
                cells.push({
                    points,
                    depth: d3.mean(points, (point) => point.depth) ?? 0,
                    dr: source.dr,
                    stability: Math.exp(
                        d3.mean(corners, (point) => Math.log(point.stability)) ?? 0,
                    ),
                    difficulty: d3.mean(corners, (point) => point.difficulty) ?? 0,
                });
            }
        }
        cells.sort((a, b) => a.depth - b.depth);
        const contourSegments = drContourSegments(surface, contourLevels());

        const bounds = projectedBounds(surface, axes, tickPoints);
        const tooltip = drawSurfaceTooltip(svg);
        const plot = svg
            .append("g")
            .attr(
                "transform",
                `translate(${width / 2 - (bounds.minX + bounds.maxX) / 2},${height - plotBottomPadding - bounds.maxY})`,
            );

        plot.selectAll("polygon.surface-cell")
            .data(cells)
            .join("polygon")
            .attr("class", "surface-cell")
            .attr("points", (cell) => cell.points.map((p) => `${p.x},${p.y}`).join(" "))
            .attr("fill", (cell) => color(cell.dr))
            .attr("stroke", "rgba(0,0,0,.18)")
            .attr("stroke-width", 0.35)
            .on("mouseenter", (event, cell) => {
                showSurfaceTooltip(tooltip, event, cell, width, height);
            })
            .on("mousemove", (event, cell) => {
                showSurfaceTooltip(tooltip, event, cell, width, height);
            })
            .on("mouseleave", () => {
                hideSurfaceTooltip(tooltip);
            });

        plot.selectAll("line.dr-contour")
            .data(contourSegments)
            .join("line")
            .attr("class", "dr-contour")
            .attr("x1", (segment) => segment.start.x)
            .attr("y1", (segment) => segment.start.y)
            .attr("x2", (segment) => segment.end.x)
            .attr("y2", (segment) => segment.end.y);

        drawAxes(plot, axes);
        drawAxisTicks(plot, tickPoints);
        drawLegend(svg, width, height, color);
        tooltip.raise();

        svg.call(
            d3
                .drag<SVGSVGElement, unknown>()
                .on("start", (event) => {
                    lastX = event.x;
                    lastY = event.y;
                })
                .on("drag", (event) => {
                    yaw += (event.x - lastX) * 0.45;
                    pitch = Math.max(
                        -70,
                        Math.min(70, pitch - (event.y - lastY) * 0.35),
                    );
                    lastX = event.x;
                    lastY = event.y;
                    renderPlot();
                }),
        );
    }

    function drawSurfaceTooltip(
        svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
    ): d3.Selection<SVGGElement, unknown, null, undefined> {
        const tooltip = svg
            .append("g")
            .attr("class", "surface-tooltip")
            .attr("visibility", "hidden");
        tooltip
            .append("rect")
            .attr("class", "surface-tooltip-bg")
            .attr("width", 188)
            .attr("height", 64)
            .attr("rx", 4);
        return tooltip;
    }

    function showSurfaceTooltip(
        tooltip: d3.Selection<SVGGElement, unknown, null, undefined>,
        event: MouseEvent,
        cell: Cell,
        width: number,
        height: number,
    ): void {
        const tooltipWidth = 188;
        const tooltipHeight = 64;
        const [pointerX, pointerY] = d3.pointer(event, svgElement);
        const x = Math.min(width - tooltipWidth - 6, pointerX + 14);
        const y = Math.min(height - tooltipHeight - 6, pointerY + 14);
        tooltip
            .attr("visibility", "visible")
            .attr("transform", `translate(${Math.max(6, x)},${Math.max(6, y)})`);
        tooltip
            .selectAll("text")
            .data(surfaceTooltipRows(cell))
            .join("text")
            .attr("class", "surface-tooltip-text")
            .attr("x", 10)
            .attr("y", (_row, index) => 17 + index * 18)
            .text((row) => row);
    }

    function hideSurfaceTooltip(
        tooltip: d3.Selection<SVGGElement, unknown, null, undefined>,
    ): void {
        tooltip.attr("visibility", "hidden");
    }

    function surfaceTooltipRows(cell: Cell): string[] {
        return [
            `Stability: ${formatStability(cell.stability)}`,
            `Difficulty: ${cell.difficulty.toFixed(2)} / 10`,
            `DR: ${formatPercent(cell.dr, 2)}`,
        ];
    }

    function axisDefinitions(): Array<[Point, Point, string]> {
        return [
            [
                { x: -1.08, y: 1.08, z: 0, stability: 0, difficulty: 0 },
                {
                    x: 1.08,
                    y: 1.08,
                    z: 0,
                    stability: 0,
                    difficulty: 0,
                },
                "S",
            ],
            [
                { x: -1.08, y: -1.08, z: 0, stability: 0, difficulty: 0 },
                {
                    x: -1.08,
                    y: 1.08,
                    z: 0,
                    stability: 0,
                    difficulty: 0,
                },
                "D",
            ],
            [
                { x: -1.08, y: 1.08, z: 0, stability: 0, difficulty: 0 },
                {
                    x: -1.08,
                    y: 1.08,
                    z: 1.75,
                    stability: 0,
                    difficulty: 0,
                },
                "DR",
            ],
        ];
    }

    function projectedBounds(
        surface: SurfacePoint[][],
        axes: Array<[Point, Point, string]>,
        ticks: AxisTick[],
    ): ProjectedBounds {
        const allProjectedPoints = [
            ...surface.flat().map(project),
            ...axes.flatMap(([start, end]) => [project(start), project(end)]),
            ...ticks.map(({ point }) => project(point)),
        ];
        return {
            minX: d3.min(allProjectedPoints, (point) => point.x) ?? 0,
            maxX: d3.max(allProjectedPoints, (point) => point.x) ?? 0,
            minY: d3.min(allProjectedPoints, (point) => point.y) ?? 0,
            maxY: d3.max(allProjectedPoints, (point) => point.y) ?? 0,
        };
    }

    function drawAxes(
        plot: d3.Selection<SVGGElement, unknown, null, undefined>,
        axes: Array<[Point, Point, string]>,
    ): void {
        for (const [start, end, label] of axes) {
            const a = project(start);
            const b = project(end);
            plot.append("line")
                .attr("class", "axis-line")
                .attr("x1", a.x)
                .attr("y1", a.y)
                .attr("x2", b.x)
                .attr("y2", b.y);
            plot.append("text")
                .attr("class", "axis-label")
                .attr("x", b.x)
                .attr("y", b.y)
                .text(label);
        }
    }

    function axisTickPoints(): AxisTick[] {
        const stabilityTicks = [0.1, 1, 10, 100, 1000].map((value) => ({
            point: {
                x: stabilityToAxis(value),
                y: 1.16,
                z: 0,
                stability: 0,
                difficulty: 0,
            },
            label: value.toString(),
            dy: 12,
        }));
        const difficultyTicks = [1, 5, 10].map((value) => ({
            point: {
                x: -1.2,
                y: difficultyToAxis(value),
                z: 0,
                stability: 0,
                difficulty: 0,
            },
            label: value.toString(),
            dx: -8,
            dy: 3,
        }));
        const drTicks = [
            retentionMin,
            (retentionMin + retentionMax) / 2,
            retentionMax,
        ].map((value) => ({
            point: {
                x: -1.2,
                y: 1.2,
                z: retentionToAxis(value),
                stability: 0,
                difficulty: 0,
            },
            label: formatPercent(value),
            dx: -10,
            dy: 3,
        }));
        return [...stabilityTicks, ...difficultyTicks, ...drTicks];
    }

    function drawAxisTicks(
        plot: d3.Selection<SVGGElement, unknown, null, undefined>,
        ticks: AxisTick[],
    ): void {
        for (const tick of ticks) {
            const point = project(tick.point);
            plot.append("text")
                .attr("class", "axis-tick")
                .attr("x", point.x)
                .attr("y", point.y)
                .attr("dx", tick.dx ?? 0)
                .attr("dy", tick.dy ?? 0)
                .attr("text-anchor", "middle")
                .text(tick.label);
        }
    }

    function contourLevels(): number[] {
        const first = Math.ceil((retentionMin + 0.000001) * 10) / 10;
        const last = Math.floor((retentionMax - 0.000001) * 10) / 10;
        return d3.range(first, last + 0.001, 0.1);
    }

    function drContourSegments(
        surface: SurfacePoint[][],
        levels: number[],
    ): ContourSegment[] {
        const segments: ContourSegment[] = [];
        for (let y = 0; y < surface.length - 1; y++) {
            for (let x = 0; x < surface[y].length - 1; x++) {
                const corners = [
                    surface[y][x],
                    surface[y][x + 1],
                    surface[y + 1][x + 1],
                    surface[y + 1][x],
                ];
                for (const level of levels) {
                    const intersections = contourIntersections(corners, level);
                    for (let index = 0; index + 1 < intersections.length; index += 2) {
                        const start = project(intersections[index]);
                        const end = project(intersections[index + 1]);
                        segments.push({
                            start,
                            end,
                            depth: (start.depth + end.depth) / 2,
                            level,
                        });
                    }
                }
            }
        }
        return segments.sort((a, b) => a.depth - b.depth);
    }

    function contourIntersections(
        corners: SurfacePoint[],
        level: number,
    ): SurfacePoint[] {
        const intersections: SurfacePoint[] = [];
        for (let index = 0; index < corners.length; index++) {
            const start = corners[index];
            const end = corners[(index + 1) % corners.length];
            if ((level - start.dr) * (level - end.dr) > 0 || start.dr === end.dr) {
                continue;
            }
            const t = (level - start.dr) / (end.dr - start.dr);
            if (t < 0 || t > 1) {
                continue;
            }
            intersections.push(interpolatePoint(start, end, t, level));
        }
        return uniquePoints(intersections);
    }

    function interpolatePoint(
        start: SurfacePoint,
        end: SurfacePoint,
        t: number,
        dr: number,
    ): SurfacePoint {
        return {
            x: start.x + (end.x - start.x) * t,
            y: start.y + (end.y - start.y) * t,
            z: start.z + (end.z - start.z) * t,
            stability: start.stability + (end.stability - start.stability) * t,
            difficulty: start.difficulty + (end.difficulty - start.difficulty) * t,
            dr,
        };
    }

    function uniquePoints(points: SurfacePoint[]): SurfacePoint[] {
        const seen = new Set<string>();
        return points.filter((point) => {
            const key = `${point.x.toFixed(5)},${point.y.toFixed(5)},${point.z.toFixed(5)}`;
            if (seen.has(key)) {
                return false;
            }
            seen.add(key);
            return true;
        });
    }

    function drawLegend(
        svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
        width: number,
        height: number,
        color: d3.ScaleSequential<string>,
    ): void {
        const legendWidth = 180;
        const legendHeight = 10;
        const x = width - legendWidth - 28;
        const y = 24;
        const gradientId = "dynamic-dr-gradient";
        const defs = svg.append("defs");
        const gradient = defs
            .append("linearGradient")
            .attr("id", gradientId)
            .attr("x1", "0%")
            .attr("x2", "100%");
        d3.range(0, 1.01, 0.1).forEach((step) => {
            gradient
                .append("stop")
                .attr("offset", `${step * 100}%`)
                .attr(
                    "stop-color",
                    color(retentionMin + step * (retentionMax - retentionMin)),
                );
        });
        svg.append("rect")
            .attr("x", x)
            .attr("y", y)
            .attr("width", legendWidth)
            .attr("height", legendHeight)
            .attr("fill", `url(#${gradientId})`);
        svg.append("text")
            .attr("class", "legend-label")
            .attr("x", x)
            .attr("y", y - 6)
            .text(formatPercent(retentionMin));
        svg.append("text")
            .attr("class", "legend-label")
            .attr("x", x + legendWidth)
            .attr("y", y - 6)
            .attr("text-anchor", "end")
            .text(formatPercent(retentionMax));
    }

    function formatPercent(value: number, digits = 1): string {
        return `${(value * 100).toFixed(digits)}%`;
    }

    function formatStability(value: number): string {
        if (value >= 100) {
            return `${value.toFixed(0)} d`;
        } else if (value >= 10) {
            return `${value.toFixed(1)} d`;
        }
        return `${value.toFixed(2)} d`;
    }

    function parsePercentInput(value: string): number {
        const normalized = value.trim().replace("%", "");
        return Number(normalized) / 100;
    }

    function stabilityToAxis(value: number): number {
        return (
            ((Math.log(value) - Math.log(0.1)) / (Math.log(1000) - Math.log(0.1)) -
                0.5) *
            2
        );
    }

    function difficultyToAxis(value: number): number {
        return ((value - 1) / 9 - 0.5) * 2;
    }

    function retentionToAxis(value: number): number {
        return ((value - retentionMin) / (retentionMax - retentionMin)) * 1.6;
    }

    function setDraftTargetDr(value: number): void {
        if (!Number.isFinite(value)) {
            return;
        }
        draftTargetDr = Math.min(selectorMax, Math.max(selectorMin, value));
        renderPlot();
    }

    function saveDraftTargetDr(): void {
        if (canPlot) {
            dispatch("saveTarget", draftTargetDr);
        }
    }

    function canPlotWithWeight(weight: number | null): boolean {
        return (
            validPolicyParams(params) &&
            validCalibration(calibrationWeights, calibrationAvgDrs) &&
            hasTargetCalibration &&
            validRetentionBounds(retentionMin, retentionMax) &&
            weight !== null
        );
    }

    function project(point: Point): ProjectedPoint {
        const yawRad = (yaw * Math.PI) / 180;
        const pitchRad = (pitch * Math.PI) / 180;
        const cy = Math.cos(yawRad);
        const sy = Math.sin(yawRad);
        const cp = Math.cos(pitchRad);
        const sp = Math.sin(pitchRad);
        const x1 = point.x * cy - point.y * sy;
        const y1 = point.x * sy + point.y * cy;
        const y2 = y1 * cp - point.z * sp;
        const z2 = y1 * sp + point.z * cp;
        return {
            x: x1 * projectionScale,
            y: y2 * projectionScale,
            depth: z2,
        };
    }

    interface Point {
        x: number;
        y: number;
        z: number;
        stability: number;
        difficulty: number;
    }

    interface SurfacePoint extends Point {
        dr: number;
    }

    interface ProjectedPoint {
        x: number;
        y: number;
        depth: number;
    }

    interface ProjectedBounds {
        minX: number;
        maxX: number;
        minY: number;
        maxY: number;
    }

    interface Cell {
        points: ProjectedPoint[];
        depth: number;
        dr: number;
        stability: number;
        difficulty: number;
    }

    interface ContourSegment {
        start: ProjectedPoint;
        end: ProjectedPoint;
        depth: number;
        level: number;
    }

    interface AxisTick {
        point: Point;
        label: string;
        dx?: number;
        dy?: number;
    }
</script>

{#snippet plotContent()}
    <div class="modal-header">
        <h5 class="modal-title">Dynamic DR Plot</h5>
        <button
            type="button"
            class="btn-close"
            aria-label="Close"
            on:click={closeRequested}
        ></button>
    </div>
    <div class="modal-body">
        <div class="plot-meta">
            <label>
                Target {targetCalibration.label}
                <input
                    type="range"
                    min={selectorMin}
                    max={selectorMax}
                    step="0.001"
                    value={draftTargetDr}
                    disabled={!hasTargetCalibration}
                    on:input={(event) =>
                        setDraftTargetDr(
                            Number((event.currentTarget as HTMLInputElement).value),
                        )}
                />
            </label>
            <input
                class="target-dr-input"
                type="text"
                inputmode="decimal"
                value={formatPercent(draftTargetDr, 2)}
                disabled={!hasTargetCalibration}
                on:change={(event) =>
                    setDraftTargetDr(
                        parsePercentInput(
                            (event.currentTarget as HTMLInputElement).value,
                        ),
                    )}
            />
            <span>
                Weight: {inferredWeight === null ? "n/a" : inferredWeight.toFixed(2)}
            </span>
            <button
                type="button"
                class="btn btn-sm btn-primary"
                disabled={!canPlot}
                on:click={saveDraftTargetDr}
            >
                Save DR
            </button>
        </div>
        <svg bind:this={svgElement} class="plot" role="img"></svg>
    </div>
{/snippet}

{#if inline}
    <section class="dynamic-dr-inline">
        {@render plotContent()}
    </section>
{:else}
    <div class="modal" tabindex="-1" use:setupModal>
        <div class="modal-dialog dynamic-dr-dialog">
            <div class="modal-content">
                {@render plotContent()}
            </div>
        </div>
    </div>
{/if}

<style>
    .dynamic-dr-inline {
        min-height: 100vh;
        background: var(--canvas);
    }

    .plot {
        width: 100%;
        aspect-ratio: 1 / 1;
        max-height: min(72vh, 680px);
        cursor: grab;
        background: var(--canvas);
    }

    .dynamic-dr-dialog {
        max-width: min(92vw, 760px);
    }

    .plot:active {
        cursor: grabbing;
    }

    .plot-meta {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 1rem;
        margin-bottom: 0.5rem;
        font-size: 0.9rem;
    }

    .plot-meta label {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 0;
    }

    .plot-meta input[type="range"] {
        width: 16rem;
    }

    .target-dr-input {
        width: 5rem;
    }

    :global(.axis-line) {
        stroke: var(--fg);
        stroke-width: 1.2;
    }

    :global(.dr-contour) {
        stroke: rgba(255, 255, 255, 0.9);
        stroke-width: 1.2;
        stroke-linecap: round;
        pointer-events: none;
    }

    :global(.axis-label),
    :global(.axis-tick),
    :global(.legend-label),
    :global(.plot-empty) {
        fill: var(--fg);
        font-size: 12px;
    }

    :global(.axis-tick) {
        font-size: 10px;
    }

    :global(.surface-tooltip) {
        pointer-events: none;
    }

    :global(.surface-tooltip-bg) {
        fill: rgba(20, 20, 20, 0.78);
        stroke: rgba(255, 255, 255, 0.28);
        stroke-width: 1;
    }

    :global(.surface-tooltip-text) {
        fill: #fff;
        font-size: 12px;
    }
</style>
