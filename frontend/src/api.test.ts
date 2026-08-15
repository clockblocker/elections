import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { demoMetadata } from "./demo";
import { DEFAULT_STATE } from "./state";

afterEach(() => vi.unstubAllGlobals());

describe("API wire adapters", () => {
  it("paginates point pages and maps snake_case coordinates", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ result_record_id: 7, party_id: 1, uik_number: "42", tik_name: "TIK 3", region_name: "Perm", registered_voters: 1000, ballots_counted: 600, party_votes: 300, turnout_percent: 60, party_percent: 50, match_status: "matched", validation_status: "valid", special_type: null, is_deg: false, flags: [] }], offset: 0, limit: 20000, total: 2, has_more: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ result_record_id: 8, party_id: 1, uik_number: "43", tik_name: null, region_name: "Perm", registered_voters: 800, ballots_counted: 400, party_votes: 160, turnout_percent: 50, party_percent: 40, match_status: "unmatched", validation_status: null, special_type: "temporary", is_deg: false, flags: [] }], offset: 20000, limit: 20000, total: 2, has_more: false }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const metadata = { ...demoMetadata, parties: [{ id: "1", name: "United Russia", shortName: "UR", color: "#d95d39" }] };
    const points = await api.points({ ...DEFAULT_STATE, partyIds: ["1"], regionIds: ["Perm"] }, metadata);
    expect(points).toHaveLength(2);
    expect(points[0]).toEqual(expect.objectContaining({ id: "7:1", uikId: "7", turnout: 60, partyShare: 50, partyName: "United Russia" }));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/v1/points?");
    expect(fetchMock.mock.calls[0][0]).toContain("party_id=1");
    expect(fetchMock.mock.calls[1][0]).toContain("offset=20000");
  });
});
