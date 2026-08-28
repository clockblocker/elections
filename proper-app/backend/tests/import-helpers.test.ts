import { describe, expect, test } from "bun:test";
import { electionConfig } from "../src/constants";
import { accountingValues, ballotOptions, parseNumberedOption, placeholders, protocolArray } from "../src/import-helpers";
import type { ElectionProtocol } from "../src/types";

const accounting = {
  "Число избирателей, внесенных в список": 0,
  "Число полученных избирательных бюллетеней": 1,
  "Число избирательных бюллетеней, выданных досрочно": 2,
  "Число избирательных бюллетеней, выданных в день голосования": 3,
  "Число избирательных бюллетеней, выданных вне помещения": 4,
  "Число погашенных избирательных бюллетеней": 5,
  "Число избирательных бюллетеней в переносных ящиках": 6,
  "Число бюллетеней в стационарных ящиках для голосования": 7,
  "Число недействительных избирательных бюллетеней": 8,
  "Число действительных избирательных бюллетеней": 9,
  "Число утраченных избирательных бюллетеней": 10,
  "Число не учтенных при получении избирательных бюллетеней": 11
};
const protocol = {
  election: "2004-president", level: "uik", ballot: "presidential", reportType: 226,
  uikTvd: "uik", accounting, votes: { "gas:candidate-vibid:1": 20 }
} as unknown as ElectionProtocol;

describe("multi-election import boundary", () => {
  test("normalizes the numbered 2021 party labels", () => {
    expect(parseNumberedOption('5. Всероссийская политическая партия "ЕДИНАЯ РОССИЯ"')).toEqual({
      position: 5,
      name: 'Всероссийская политическая партия "ЕДИНАЯ РОССИЯ"',
      shortName: "Единая Россия",
      color: "#315f9d"
    });
  });

  test("resolves catalog-backed presidential candidates", () => {
    expect(ballotOptions(protocol, electionConfig("2004-president"), {
      candidates: [{ voteKey: "gas:candidate-vibid:1", fullName: "Путин Владимир Владимирович" }]
    })[0]).toMatchObject({ position: 1, name: "Путин Владимир Владимирович", shortName: "Путин" });
  });

  test("maps historical accounting label variants in database order", () => {
    expect(accountingValues(protocol)).toEqual(Array.from({ length: 12 }, (_, index) => index));
  });

  test("builds positional bulk placeholders", () => {
    expect(placeholders(2, 3)).toBe("($1,$2,$3),($4,$5,$6)");
  });

  test("rejects a protocol for the wrong generated ballot", () => {
    const config = electionConfig("2004-president");
    expect(() => protocolArray({ data: [{ ...protocol, ballot: "party" }] }, "bad.ts", config)).toThrow();
  });
});
