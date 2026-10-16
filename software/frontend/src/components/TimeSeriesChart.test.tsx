// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getInstanceByDom, type EChartsType } from "echarts/core";
import { breakCurveAtGaps, TimeSeriesChart, type PlotSeries } from "./TimeSeriesChart";

// Exercise the real ECharts tooltip/model lifecycle. SVG only replaces the
// browser's Canvas drawing surface, which jsdom does not implement.
vi.mock("echarts/core", async (importOriginal) => {
  const core = await importOriginal<typeof import("echarts/core")>();
  const { SVGRenderer } = await import("echarts/renderers");
  core.use([SVGRenderer]);
  core.setPlatformAPI({ measureText: (text) => ({ width: text.length * 7 }) });
  return { ...core, init: (host: HTMLElement) => core.init(host, undefined, { renderer: "svg", width: 800, height: host.classList.contains("chart-overview-canvas") ? 84 : 475 }) };
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const first: PlotSeries = { id: "first", name: "A 采集", unit: "N·m", points: [[0, 1], [1, 2]] };
const second: PlotSeries = { id: "second", name: "B 采集", unit: "N·m", points: [[0, 3], [1, 4]] };
const props = { domain: { start: 0, end: 1 }, viewport: { start: 0, end: 1 }, references: [], onViewport: vi.fn(), loading: false, resolution: "原始分辨率" };

it("invalidates a hovered axis tooltip before asynchronous series removal and restores hover on current data", async () => {
  const view = render(<TimeSeriesChart {...props} series={[first, second]} />);
  const host = view.container.querySelector<HTMLElement>(".time-series-canvas")!;
  const instance = getInstanceByDom(host)!;
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("A 采集");
  expect(host.textContent).toContain("B 采集");

  // A leaves the viewport while B stays under the pointer. ECharts preserves
  // B's series index, so cached tooltip indices for A are no longer valid.
  view.rerender(<TimeSeriesChart {...props} series={[second]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("B 采集");
  expect(host.textContent).not.toContain("A 采集");

  // An empty response followed by fresh data must not resurrect old indices.
  view.rerender(<TimeSeriesChart {...props} series={[]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  view.rerender(<TimeSeriesChart {...props} series={[first, second]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("A 采集");
  expect(host.textContent).toContain("B 采集");
});

const fullDomain = { start: 0, end: 100 };
const fullSeries: PlotSeries = { id: "full", name: "全程", unit: "N·m", points: [[0, 1], [20, 2], [40, 3], [60, 2], [80, 4], [100, 2]] };
function mainInstance(container: HTMLElement) { return getInstanceByDom(container.querySelector<HTMLElement>(".time-series-canvas")!)!; }
function mainSeries(instance: EChartsType) { return instance.getOption().series as { id: string; data: [number, number | null][]; connectNulls: boolean; xAxisIndex: number; yAxisIndex: number; markPoint?: { data: { name: string; coord: number[] }[] } }[]; }
function zoom(instance: EChartsType, start: number, end: number) { instance.dispatchAction({ type: "dataZoom", dataZoomId: "x-inside", startValue: start, endValue: end }); }
async function flushViewport() { await act(async () => { await vi.advanceTimersByTimeAsync(300); }); }

it("preserves every explicit gap and sampled null without mutating input", () => {
  const points: PlotSeries["points"] = [[0, 1], [2, 3], [3, null], [5, 5], [8, 8], [10, 3]];
  const copy = structuredClone(points);
  expect(breakCurveAtGaps(points, [{ start: 1, end: 2 }, { start: 6, end: 7 }])).toEqual([[0, 1], [1.5, null], [2, 3], [3, null], [5, 5], [6.5, null], [8, 8], [10, 3]]);
  expect(points).toEqual(copy);
  expect(breakCurveAtGaps([[0, 1], [100, 2]])).toEqual([[0, 1], [100, 2]]);
});

it("debounces the final viewport and does not repeat an already committed event", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => { zoom(instance, 10, 70); zoom(instance, 20, 60); zoom(instance, 20, 60); });
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 20, end: 60 });
  act(() => zoom(instance, 20, 60));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
});

it("cancels pending viewport work on reset, externally changed domain, and unmount", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => zoom(instance, 20, 60));
  fireEvent.click(screen.getByRole("button", { name: "重置视图" }));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith(fullDomain);
  onViewport.mockClear();
  act(() => zoom(instance, 20, 60));
  view.rerender(<TimeSeriesChart {...props} domain={{ start: 0, end: 50 }} viewport={{ start: 0, end: 50 }} onViewport={onViewport} series={[fullSeries]} />);
  await flushViewport();
  expect(onViewport).not.toHaveBeenCalled();
  act(() => zoom(instance, 10, 30));
  view.unmount();
  await flushViewport();
  expect(onViewport).not.toHaveBeenCalled();
});

it("validates manual axes, clips time to the domain, and resets Y zoom", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  fireEvent.click(screen.getByRole("button", { name: "X 范围" }));
  fireEvent.change(screen.getByLabelText("时间起点"), { target: { value: "80" } });
  fireEvent.change(screen.getByLabelText("时间终点"), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  expect(screen.getByRole("alert").textContent).toContain("有效时间范围");
  expect(onViewport).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("时间起点"), { target: { value: "-10" } });
  fireEvent.change(screen.getByLabelText("时间终点"), { target: { value: "40" } });
  fireEvent.change(screen.getByLabelText("纵轴下限"), { target: { value: "-5" } });
  fireEvent.change(screen.getByLabelText("纵轴上限"), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 0, end: 40 });
  const instance = mainInstance(view.container);
  expect((instance.getOption().yAxis as { min: number; max: number }[])[0]).toMatchObject({ min: -5, max: 8 });
  fireEvent.click(screen.getByRole("button", { name: "重置视图" }));
  expect((instance.getOption().yAxis as { min?: number }[])[0].min).toBeUndefined();
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(2);
});

it("uses separate linked grids in facets and restores axes on return to overlay", () => {
  const view = render(<TimeSeriesChart {...props} series={[first, second]} layout="facets" />);
  const instance = mainInstance(view.container);
  expect((instance.getOption().grid as unknown[])).toHaveLength(2);
  expect(mainSeries(instance).map((item) => [item.xAxisIndex, item.yAxisIndex])).toEqual([[0, 0], [1, 1]]);
  expect((instance.getOption().axisPointer as { link: unknown[] }[])[0].link).toEqual([{ xAxisIndex: [0, 1] }]);
  fireEvent.click(screen.getByRole("checkbox", { name: "联动游标" }));
  expect((instance.getOption().axisPointer as { link: unknown[] }[])[0].link).toEqual([]);
  fireEvent.click(screen.getByRole("checkbox", { name: "显示曲线 A 采集" }));
  expect((instance.getOption().grid as unknown[])).toHaveLength(1);
  expect(mainSeries(instance).find((item) => item.id === "first")?.data).toEqual([]);
  view.rerender(<TimeSeriesChart {...props} series={[first, second]} layout="overlay" />);
  expect((instance.getOption().grid as unknown[])).toHaveLength(1);
  expect(mainSeries(instance).every((item) => item.yAxisIndex === 0)).toBe(true);
});

it("keeps the complete overview data independent of the current narrow window, including gaps", async () => {
  const onViewport = vi.fn();
  const overview: PlotSeries = { ...fullSeries, gaps: [{ start: 5, end: 10 }, { start: 85, end: 90 }] };
  const current: PlotSeries = { ...fullSeries, points: [[40, 3], [60, 2]] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={{ start: 40, end: 60 }} series={[current]} overviewSeries={[overview]} onViewport={onViewport} />);
  const instance = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  expect(mainSeries(instance)[0].data).toContainEqual([7.5, null]);
  expect(mainSeries(instance)[0].data).toContainEqual([87.5, null]);
  expect(mainSeries(instance)[0].connectNulls).toBe(false);
  act(() => instance.dispatchAction({ type: "dataZoom", dataZoomId: "overview-slider", startValue: 10, endValue: 30 }));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 10, end: 30 });
  expect(instance.convertToPixel({ xAxisIndex: 0 }, 0)).toBeCloseTo(58);
  expect(instance.convertToPixel({ xAxisIndex: 0 }, 100)).toBeCloseTo(752);
  expect(mainSeries(instance)[0].data.at(-1)).toEqual([100, 2]);
});

it("renders only explicitly supplied markers, omitting markers inside a declared gap", () => {
  const marked: PlotSeries = { ...fullSeries, gaps: [{ start: 30, end: 50 }], anomalies: [{ time: 20, value: 2, label: "采样越限" }, { time: 40, value: 3, label: "不得显示" }] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[marked]} />);
  const plotted = mainSeries(mainInstance(view.container))[0];
  expect(plotted.markPoint?.data).toHaveLength(1);
  expect(plotted.markPoint?.data[0]).toMatchObject({ name: "采样越限", coord: [20, 2], value: 2 });
  expect(plotted.data).toContainEqual([40, null]);
  const host = view.container.querySelector<HTMLElement>(".time-series-canvas")!;
  expect(host.textContent).toContain("采样越限");
  expect(host.textContent).toContain("缺失段");
  expect(host.textContent).not.toContain("不得显示");
});

it("rejects mixed units and hides every incompatible line", () => {
  const view = render(<TimeSeriesChart {...props} series={[first, { ...second, unit: "V" }]} />);
  expect(screen.getByRole("alert").textContent).toContain("单位不一致");
  expect(mainSeries(mainInstance(view.container))).toEqual([]);
});

it("formats fractional relative-time ticks in both charts without overriding absolute-time formatting", () => {
  const domain = { start: 20.056497175141246, end: 60.5461 };
  const view = render(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[fullSeries]} />);
  const main = mainInstance(view.container);
  const overview = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  const formatter = (instance: EChartsType) => (instance.getOption().xAxis as { axisLabel: { formatter?: (value: number) => string } }[])[0].axisLabel.formatter;
  expect(formatter(main)?.(domain.start)).toBe("20.0565");
  expect(formatter(overview)?.(domain.start)).toBe("20.0565");
  expect(view.container.textContent).not.toContain("20.056497175141246");
  view.rerender(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[fullSeries]} absolute />);
  expect(formatter(main)).toBeUndefined();
  expect(formatter(overview)).toBeUndefined();
});

it("removes old limits, markers, and shaded gaps when their inputs are disabled", () => {
  const marked: PlotSeries = { ...fullSeries, gaps: [{ start: 30, end: 50 }], anomalies: [{ time: 20, value: 2, label: "超限" }] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[marked]} references={[{ id: "limit", name: "上限", value: 3 }]} />);
  const instance = mainInstance(view.container);
  view.rerender(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[fullSeries]} />);
  const plotted = instance.getOption().series as { markLine?: { data?: unknown[] }; markPoint?: { data?: unknown[] }; markArea?: { data?: unknown[] } }[];
  expect(plotted[0].markLine?.data ?? []).toEqual([]);
  expect(plotted[0].markPoint?.data ?? []).toEqual([]);
  expect(plotted[0].markArea?.data ?? []).toEqual([]);
});

function dragOnZrender(instance: EChartsType, from: number, to: number) {
  const start = instance.convertToPixel({ xAxisIndex: 0 }, from) as number;
  const end = instance.convertToPixel({ xAxisIndex: 0 }, to) as number;
  const y = instance.convertToPixel({ yAxisIndex: 0 }, 2.5) as number;
  for (const [type, x] of [["mousedown", start], ["mousemove", end], ["mouseup", end]] as const) {
    instance.getZr().trigger(type, { offsetX: x, offsetY: y, event: { button: 0, preventDefault() {}, stopPropagation() {} } });
  }
}

it("routes a real ECharts brush controller drag to a viewport request and clears the selection", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  fireEvent.click(screen.getByRole("button", { name: "框选缩放" }));
  const instance = mainInstance(view.container);
  const brushAreas: unknown[][] = [];
  instance.on("brush", (event: unknown) => { brushAreas.push((event as { areas: unknown[] }).areas); });
  act(() => dragOnZrender(instance, 20, 60));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
  expect(onViewport.mock.calls[0][0].start).toBeCloseTo(20);
  expect(onViewport.mock.calls[0][0].end).toBeCloseTo(60);
  expect(brushAreas.at(-1)).toEqual([]);
});

it("routes a real ECharts roam controller pan to the shifted time window", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={{ start: 20, end: 80 }} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => dragOnZrender(instance, 40, 50));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
  expect(onViewport.mock.calls[0][0].start).toBeCloseTo(10);
  expect(onViewport.mock.calls[0][0].end).toBeCloseTo(70);
});
