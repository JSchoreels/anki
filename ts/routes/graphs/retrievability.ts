// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/* eslint
@typescript-eslint/no-explicit-any: "off",
 */

import type { GraphsResponse } from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedNumber } from "@tslib/i18n";
import type { Bin, ScaleLinear } from "d3";
import { axisBottom, axisLeft, bin, max, pointer, scaleLinear, select, sum } from "d3";

import type { GraphBounds, SearchDispatch, TableDatum } from "./graph-helpers";
import { numericMap, setDataAvailable } from "./graph-helpers";
import { clickableClass } from "./graph-styles";
import { getAdjustedScaleAndTicks, percentageRangeMinMax } from "./percentageRange";
import { hideTooltip, showTooltip } from "./tooltip-utils.svelte";

type CountBin = Bin<[number, number], number>;
type ProtobufRetrievabilitySeries = NonNullable<
    NonNullable<GraphsResponse["retrievability"]>["fsrs"]
>;

interface SeriesData {
    retrievability: Map<number, number>;
    average: number;
    sumByCard: number;
    sumByNote: number;
}

interface NamedSeriesData {
    key: string;
    label: string;
    colour: string;
    data: SeriesData;
}

export interface GraphData {
    active: SeriesData;
    fsrs: SeriesData | null;
    rwkv: SeriesData | null;
}

export interface RetrievabilityHistogramSeries {
    key: string;
    label: string;
    colour: string;
    bins: CountBin[];
    total: number;
}

export interface RetrievabilityHistogramData {
    scale: ScaleLinear<number, number>;
    series: RetrievabilityHistogramSeries[];
    hoverText: (index: number) => string;
    onClick: ((data: CountBin) => void) | null;
    xTickFormat: (d: number) => string;
}

const fsrsColour = "#2f9e44";
const rwkvColour = "#d6a21d";

export function shouldShowRetrievabilityGraph(data: GraphsResponse | null): boolean {
    return Boolean(data?.fsrs || data?.retrievability?.rwkv);
}

function gatherSeries(data: ProtobufRetrievabilitySeries): SeriesData {
    return {
        retrievability: numericMap(data.retrievability),
        average: data.average,
        sumByCard: data.sumByCard,
        sumByNote: data.sumByNote,
    };
}

function gatherOptionalSeries(
    data: ProtobufRetrievabilitySeries | undefined,
): SeriesData | null {
    if (!data || !Object.keys(data.retrievability).length) {
        return null;
    }

    return gatherSeries(data);
}

export function gatherData(data: GraphsResponse): GraphData {
    const retrievability = data.retrievability!;
    return {
        active: gatherSeries(retrievability),
        fsrs: gatherOptionalSeries(retrievability.fsrs),
        rwkv: gatherOptionalSeries(retrievability.rwkv),
    };
}

function makeQuery(start: number, end: number): string {
    const fromQuery = `"prop:r>=${start / 100}"`;
    let tillQuery = `"prop:r<${(end + 1) / 100}"`;
    if (end === 99) {
        tillQuery = tillQuery.replace("<", "<=");
    }
    return `${fromQuery} AND ${tillQuery}`;
}

function combinedRetrievability(series: NamedSeriesData[]): Map<number, number> {
    const combined = new Map<number, number>();
    for (const item of series) {
        for (const [retrievability, count] of item.data.retrievability) {
            combined.set(retrievability, (combined.get(retrievability) ?? 0) + count);
        }
    }
    return combined;
}

function binValue(bin: CountBin): number {
    return sum(bin, (entry) => entry[1]);
}

function tableLabel(base: string, label: string, includeSeriesLabel: boolean): string {
    return includeSeriesLabel ? `${base} (${label})` : base;
}

function knowledgeValue(data: SeriesData): string {
    return `${tr.statisticsCards({ cards: +data.sumByCard.toFixed(0) })} / ${
        tr.statisticsNotes({ notes: +data.sumByNote.toFixed(0) })
    }`;
}

export function prepareData(
    data: GraphData,
    dispatch: SearchDispatch,
    browserLinksSupported: boolean,
    quantile?: number,
): [RetrievabilityHistogramData | null, TableDatum[]] {
    const explicitSeries: NamedSeriesData[] = [
        data.fsrs && {
            key: "fsrs",
            label: "FSRS",
            colour: fsrsColour,
            data: data.fsrs,
        },
        data.rwkv && {
            key: "rwkv",
            label: "RWKV",
            colour: rwkvColour,
            data: data.rwkv,
        },
    ].filter(Boolean) as NamedSeriesData[];
    const displaySeries = explicitSeries.length
        ? explicitSeries
        : [
            {
                key: "retrievability",
                label: "",
                colour: fsrsColour,
                data: data.active,
            },
        ];
    const allRetrievability = combinedRetrievability(displaySeries);
    if (!allRetrievability.size) {
        return [null, []];
    }

    const [xMin, xMax] = percentageRangeMinMax(allRetrievability, quantile);
    const desiredBars = 20;
    const [scale, ticks] = getAdjustedScaleAndTicks(xMin, xMax, desiredBars);
    const makeBins = bin<[number, number], number>()
        .value((entry) => entry[0])
        .domain(scale.domain() as [number, number])
        .thresholds(ticks);
    const histogramSeries = displaySeries.map((series) => {
        const bins = makeBins(series.data.retrievability.entries() as any);
        return {
            key: series.key,
            label: series.label,
            colour: series.colour,
            bins,
            total: sum(bins, binValue),
        };
    });

    function hoverText(index: number): string {
        const bin = histogramSeries[0].bins[index];
        const percent = `${bin.x0}%-${bin.x1}%`;
        return histogramSeries
            .map((series) => {
                const prefix = series.label ? `${series.label}: ` : "";
                return `${prefix}${
                    tr.statisticsRetrievabilityTooltip({
                        cards: binValue(series.bins[index]),
                        percent,
                    })
                }`;
            })
            .join("<br>");
    }

    function onClick(bin: CountBin): void {
        const start = bin.x0!;
        const end = bin.x1! - 1;
        const query = makeQuery(start, end);
        dispatch("search", { query });
    }

    const xTickFormat = (num: number): string => localizedNumber(num, 0) + "%";
    const includeSeriesLabel = displaySeries.length > 1;
    const tableData = displaySeries.flatMap((series) => [
        {
            label: tableLabel(
                tr.statisticsAverageRetrievability(),
                series.label,
                includeSeriesLabel,
            ),
            value: xTickFormat(series.data.average),
        },
        {
            label: tableLabel(
                tr.statisticsEstimatedTotalKnowledge(),
                series.label,
                includeSeriesLabel,
            ),
            value: knowledgeValue(series.data),
        },
    ]);

    return [
        {
            scale,
            series: histogramSeries,
            hoverText,
            onClick: browserLinksSupported ? onClick : null,
            xTickFormat,
        },
        tableData,
    ];
}

export function retrievabilityHistogramGraph(
    svgElem: SVGElement,
    bounds: GraphBounds,
    data: RetrievabilityHistogramData | null,
): void {
    const svg = select(svgElem);
    const trans = svg.transition().duration(600) as any;
    const axisTickFormat = (n: number): string => localizedNumber(n);

    svg.select(".y2-ticks").selectAll("*").remove();

    if (!data) {
        setDataAvailable(svg, false);
        return;
    } else {
        setDataAvailable(svg, true);
    }

    const x = data.scale.range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    svg.select<SVGGElement>(".x-ticks")
        .call((selection) =>
            selection.transition(trans).call(
                axisBottom(x)
                    .ticks(7)
                    .tickSizeOuter(0)
                    .tickFormat(data.xTickFormat as any),
            )
        )
        .attr("direction", "ltr");

    const yMax = max(
        data.series.flatMap((series) => series.bins),
        binValue,
    )!;
    const y = scaleLinear()
        .range([bounds.height - bounds.marginBottom, bounds.marginTop])
        .domain([0, yMax])
        .nice();
    svg.select<SVGGElement>(".y-ticks")
        .call((selection) =>
            selection.transition(trans).call(
                axisLeft(y)
                    .ticks(bounds.height / 50)
                    .tickSizeOuter(0)
                    .tickFormat(axisTickFormat as any),
            )
        )
        .attr("direction", "ltr");

    function barWidth(d: CountBin): number {
        return Math.max(0, x(d.x1!) - x(d.x0!) - 1);
    }

    const barData = data.series.flatMap((series) => series.bins.map((bin) => ({ bin, series })));
    const fillOpacity = data.series.length > 1 ? 0.65 : 1;
    const updateBar = (sel: any): any => {
        return sel
            .attr("width", ({ bin }: any) => barWidth(bin))
            .transition(trans)
            .attr("x", ({ bin }: any) => x(bin.x0!))
            .attr("y", ({ bin }: any) => y(binValue(bin))!)
            .attr("height", ({ bin }: any) => y(0)! - y(binValue(bin))!)
            .attr("fill", ({ series }: any) => series.colour)
            .attr("fill-opacity", fillOpacity);
    };

    svg.select("g.bars")
        .selectAll("rect")
        .data(barData, (d: any) => `${d.series.key}:${d.bin.x0}`)
        .join(
            (enter) =>
                enter
                    .append("rect")
                    .attr("rx", 1)
                    .attr("x", ({ bin }: any) => x(bin.x0!))
                    .attr("y", y(0)!)
                    .attr("height", 0)
                    .call(updateBar),
            (update) => update.call(updateBar),
            (remove) => remove.call((remove) => remove.transition(trans).attr("height", 0).attr("y", y(0)!)),
        );

    const hoverData = data.series[0].bins.map((bin, index) => ({ bin, index }));
    const hoverzone = svg
        .select("g.hover-columns")
        .selectAll("rect")
        .data(hoverData)
        .join("rect")
        .attr("x", ({ bin }) => x(bin.x0!))
        .attr("y", () => y(yMax))
        .attr("width", ({ bin }) => barWidth(bin))
        .attr("height", () => y(0) - y(yMax))
        .on("mousemove", (event: MouseEvent, { index }) => {
            const [x, y] = pointer(event, document.body);
            showTooltip(data.hoverText(index), x, y);
        })
        .on("mouseout", hideTooltip);

    if (data.onClick) {
        hoverzone
            .filter(({ index }) => data.series.some((series) => binValue(series.bins[index]) > 0))
            .attr("class", clickableClass)
            .on("click", (_event, { bin }) => data.onClick!(bin));
    }
}
