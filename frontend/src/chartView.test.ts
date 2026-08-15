import { describe, expect, it } from "vitest";
import { boundChartView, FULL_CHART_VIEW } from "./chartView";

describe("chart view bounds", () => {
  it("does not zoom out beyond the percentage domain", () => {
    expect(boundChartView({ x0: -50, x1: 150, y0: -25, y1: 125 })).toEqual(FULL_CHART_VIEW);
  });

  it("preserves the zoom span while constraining a pan at every edge", () => {
    expect(boundChartView({ x0: -30, x1: 20, y0: 80, y1: 130 })).toEqual({ x0: 0, x1: 50, y0: 50, y1: 100 });
    expect(boundChartView({ x0: 80, x1: 130, y0: -30, y1: 20 })).toEqual({ x0: 50, x1: 100, y0: 0, y1: 50 });
  });

  it("keeps a near-edge selection visible without shrinking its span", () => {
    expect(boundChartView({ x0: -7, x1: 7, y0: 93, y1: 107 })).toEqual({ x0: 0, x1: 14, y0: 86, y1: 100 });
  });
});
