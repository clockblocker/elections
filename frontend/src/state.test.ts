import { describe, expect, it } from "vitest";
import { parseAnalyticalState, serializeAnalyticalState } from "./state";

describe("analytical URL state", () => {
  it("round trips filters and a selection", () => {
    const state = parseAnalyticalState("?party=ldpr&party=new-people&region=perm&special=deg&match=partial&turnoutMin=42&resultMax=80&selected=uik-9%3Aldpr");
    expect(parseAnalyticalState(serializeAnalyticalState(state))).toEqual(state);
  });

  it("bounds invalid ranges and keeps United Russia as the default", () => {
    const state = parseAnalyticalState("?turnoutMin=-5&turnoutMax=200&resultMin=nope");
    expect(state.partyIds).toEqual(["united-russia"]);
    expect(state.turnoutMin).toBe(0);
    expect(state.turnoutMax).toBe(100);
    expect(state.resultMin).toBe(0);
  });
});
