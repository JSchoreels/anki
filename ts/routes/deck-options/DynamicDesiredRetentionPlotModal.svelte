<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as d3 from "d3";
    import Modal from "bootstrap/js/dist/modal";

    import {
        costWeightForAverageDr,
        evaluateDynamicDesiredRetention,
        validCalibration,
        validPolicyParams,
    } from "./dynamic-desired-retention";

    export let modal: Modal | null = null;
    export let params: number[] = [];
    export let calibrationWeights: number[] = [];
    export let calibrationAvgDrs: number[] = [];
    export let targetAverageDr = 0.9;

    let svgElement: SVGSVGElement;
    let yaw = -42;
    let pitch = 24;
    let lastX = 0;
    let lastY = 0;

    $: inferredWeight = costWeightForAverageDr(
        targetAverageDr,
        calibrationWeights,
        calibrationAvgDrs,
    );
    $: canPlot = validPolicyParams(params)
        && validCalibration(calibrationWeights, calibrationAvgDrs)
        && inferredWeight !== null;
    $: if (svgElement) {
        renderPlot();
    }

    function setupModal(node: HTMLDivElement) {
        modal = new Modal(node);
        node.addEventListener("shown.bs.modal", renderPlot);
        return {
            destroy() {
                node.removeEventListener("shown.bs.modal", renderPlot);
                modal?.dispose();
                modal = null;
            },
        };
    }

    function renderPlot(): void {
        const svg = d3.select(svgElement);
        svg.selectAll("*").remove();
        const width = svgElement.clientWidth || 860;
        const height = svgElement.clientHeight || 560;
        svg.attr("viewBox", `0 0 ${width} ${height}`);

        if (!canPlot || inferredWeight === null) {
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
        const surface: Point[][] = [];
        for (let y = 0; y < difficultyCount; y++) {
            const difficulty = 1 + (9 * y) / (difficultyCount - 1);
            const row: Point[] = [];
            for (let x = 0; x < stabilityCount; x++) {
                const stability = Math.exp(
                    logSMin + ((logSMax - logSMin) * x) / (stabilityCount - 1),
                );
                row.push({
                    x: (x / (stabilityCount - 1) - 0.5) * 2,
                    y: (y / (difficultyCount - 1) - 0.5) * 2,
                    z: (evaluateDynamicDesiredRetention(
                        params,
                        stability,
                        difficulty,
                        inferredWeight,
                    ) - 0.3) / 0.695 * 1.6,
                    stability,
                    difficulty,
                });
            }
            surface.push(row);
        }

        const projected = surface.map((row) => row.map(project));
        const color = d3.scaleSequential(d3.interpolateViridis).domain([0.3, 0.995]);
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
                const dr = evaluateDynamicDesiredRetention(
                    params,
                    source.stability,
                    source.difficulty,
                    inferredWeight,
                );
                cells.push({
                    points,
                    depth: d3.mean(points, (point) => point.depth) ?? 0,
                    dr,
                });
            }
        }
        cells.sort((a, b) => a.depth - b.depth);

        const plot = svg.append("g")
            .attr("transform", `translate(${width / 2},${height / 2 + 28})`);

        plot.selectAll("polygon.surface-cell")
            .data(cells)
            .join("polygon")
            .attr("class", "surface-cell")
            .attr("points", (cell) => cell.points.map((p) => `${p.x},${p.y}`).join(" "))
            .attr("fill", (cell) => color(cell.dr))
            .attr("stroke", "rgba(0,0,0,.18)")
            .attr("stroke-width", 0.35);

        drawAxes(plot);
        drawLegend(svg, width, height, color);

        svg.call(
            d3.drag<SVGSVGElement, unknown>()
                .on("start", (event) => {
                    lastX = event.x;
                    lastY = event.y;
                })
                .on("drag", (event) => {
                    yaw += (event.x - lastX) * 0.45;
                    pitch = Math.max(-70, Math.min(70, pitch - (event.y - lastY) * 0.35));
                    lastX = event.x;
                    lastY = event.y;
                    renderPlot();
                }),
        );
    }

    function drawAxes(plot: d3.Selection<SVGGElement, unknown, null, undefined>): void {
        const axes: Array<[Point, Point, string]> = [
            [{ x: -1.08, y: 1.08, z: 0, stability: 0, difficulty: 0 }, {
                x: 1.08,
                y: 1.08,
                z: 0,
                stability: 0,
                difficulty: 0,
            }, "S"],
            [{ x: -1.08, y: -1.08, z: 0, stability: 0, difficulty: 0 }, {
                x: -1.08,
                y: 1.08,
                z: 0,
                stability: 0,
                difficulty: 0,
            }, "D"],
            [{ x: -1.08, y: 1.08, z: 0, stability: 0, difficulty: 0 }, {
                x: -1.08,
                y: 1.08,
                z: 1.75,
                stability: 0,
                difficulty: 0,
            }, "DR"],
        ];
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

    function drawLegend(
        svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
        width: number,
        height: number,
        color: d3.ScaleSequential<string>,
    ): void {
        const legendWidth = 180;
        const legendHeight = 10;
        const x = width - legendWidth - 28;
        const y = height - 38;
        const gradientId = "dynamic-dr-gradient";
        const defs = svg.append("defs");
        const gradient = defs.append("linearGradient")
            .attr("id", gradientId)
            .attr("x1", "0%")
            .attr("x2", "100%");
        d3.range(0, 1.01, 0.1).forEach((step) => {
            gradient.append("stop")
                .attr("offset", `${step * 100}%`)
                .attr("stop-color", color(0.3 + step * 0.695));
        });
        svg.append("rect")
            .attr("x", x)
            .attr("y", y)
            .attr("width", legendWidth)
            .attr("height", legendHeight)
            .attr("fill", `url(#${gradientId})`);
        svg.append("text").attr("class", "legend-label").attr("x", x).attr("y", y - 6).text("30%");
        svg.append("text")
            .attr("class", "legend-label")
            .attr("x", x + legendWidth)
            .attr("y", y - 6)
            .attr("text-anchor", "end")
            .text("99.5%");
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
        const scale = 190;
        return {
            x: x1 * scale,
            y: y2 * scale,
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

    interface ProjectedPoint {
        x: number;
        y: number;
        depth: number;
    }

    interface Cell {
        points: ProjectedPoint[];
        depth: number;
        dr: number;
    }
</script>

<div class="modal" tabindex="-1" use:setupModal>
    <div class="modal-dialog modal-xl">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Dynamic DR Plot</h5>
                <button
                    type="button"
                    class="btn-close"
                    aria-label="Close"
                    on:click={() => modal?.hide()}
                ></button>
            </div>
            <div class="modal-body">
                <div class="plot-meta">
                    <span>Target Avg ADR DR: {(targetAverageDr * 100).toFixed(1)}%</span>
                    <span>Weight: {inferredWeight === null ? "n/a" : inferredWeight.toFixed(2)}</span>
                </div>
                <svg bind:this={svgElement} class="plot" role="img"></svg>
            </div>
        </div>
    </div>
</div>

<style>
    .plot {
        width: 100%;
        height: min(68vh, 620px);
        cursor: grab;
        background: var(--canvas);
    }

    .plot:active {
        cursor: grabbing;
    }

    .plot-meta {
        display: flex;
        gap: 1rem;
        margin-bottom: .5rem;
        font-size: .9rem;
    }

    :global(.axis-line) {
        stroke: var(--fg);
        stroke-width: 1.2;
    }

    :global(.axis-label),
    :global(.legend-label),
    :global(.plot-empty) {
        fill: var(--fg);
        font-size: 12px;
    }
</style>
