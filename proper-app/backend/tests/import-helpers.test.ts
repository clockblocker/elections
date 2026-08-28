import { describe, expect, test } from "bun:test";
import { accountingValues, parseOption, placeholders, protocolArray } from "../src/import-helpers";
import { ACCOUNTING_KEYS } from "../src/constants";
import type { PartyProtocol } from "../src/types";

const accounting = Object.fromEntries(Object.values(ACCOUNTING_KEYS).map((key, index) => [key, index]));
const protocol = { election: "2021-duma", ballot: "party", reportType: 242, accounting } as unknown as PartyProtocol;

describe("2021 import boundary", () => {
  test("normalizes party labels", () => {
    expect(parseOption('5. Всероссийская политическая партия "ЕДИНАЯ РОССИЯ"')).toEqual({
      position: 5,
      name: 'Всероссийская политическая партия "ЕДИНАЯ РОССИЯ"',
      shortName: "Единая Россия",
      color: "#315f9d"
    });
  });

  test("maps all twelve accounting fields in database order", () => {
    expect(accountingValues(protocol)).toEqual(Array.from({ length: 12 }, (_, index) => index));
  });

  test("builds positional bulk placeholders", () => {
    expect(placeholders(2, 3)).toBe("($1,$2,$3),($4,$5,$6)");
  });

  test("rejects a wrong generated ballot", () => {
    expect(() => protocolArray({ data: [{ ...protocol, ballot: "single-member" }] }, "bad.ts")).toThrow();
  });
});
