import { describe, expect, test } from "bun:test";
import { parseId, regionFilters } from "../src/http";

describe("HTTP query boundary", () => {
  test("parses unique repeated and comma-separated regions", () => {
    const params = new URLSearchParams("region=77,78&region=77&region=50");
    expect(regionFilters(params)).toEqual(["77", "78", "50"]);
  });

  test("accepts only positive safe identifiers", () => {
    expect(parseId("42", "option")).toBe(42);
    expect(() => parseId("0", "option")).toThrow("positive integer");
    expect(() => parseId("hello", "option")).toThrow("positive integer");
  });
});
