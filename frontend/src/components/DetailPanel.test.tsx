import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Point, PrecinctDetail } from "../types";
import { DetailPanel } from "./DetailPanel";

const point: Point = {
  id: "42:candidate:900", uikId: "42", uikNumber: "123", tikId: "tik", tikName: "Central TIK", regionId: "Moscow", regionName: "Moscow", partyId: "candidate:900", partyName: "Candidate Winner", ballotId: "301", ballotKind: "single_member", districtId: "77", candidateId: "900", affiliation: "Example Party", winner: true, registeredVoters: 1000, ballotsIssued: 500, turnout: 50, partyVotes: 320, partyShare: 64, specialFlags: [], matchStatus: "matched",
};

const detail: PrecinctDetail = {
  id: "42", uikNumber: "123", tikName: "Central TIK", regionName: "Moscow", districtName: "OIK 1", accounting: { registeredVoters: 1000 }, partyResults: [], candidateResults: [{ candidateId: "900", candidateName: "Candidate Winner", affiliation: "Example Party", votes: 320, share: 64, winner: true }], protocols: [
    { resultRecordId: "41", ballotKind: "party_list", ballotName: "Federal party list", turnout: 50, partyResults: [{ partyId: "7", partyName: "Example Party", votes: 300, share: 60 }], candidateResults: [], sources: [] },
    { resultRecordId: "42", ballotKind: "single_member", ballotName: "OIK 1 ballot", turnout: 50, partyResults: [], candidateResults: [{ candidateId: "900", candidateName: "Candidate Winner", affiliation: "Example Party", votes: 320, share: 64, winner: true }], sources: [] },
  ], specialFlags: [], matchStatus: "matched", matchingMethod: "gas_vybory_id", gasVyboryId: "4014005258649", gasResolutionStatus: "resolved", sources: [],
};

describe("DetailPanel", () => {
  it("shows linked party-list and single-member UIK protocols", () => {
    render(<DetailPanel point={point} detail={detail} loading={false} error={null} onClose={() => {}} />);
    expect(screen.getByText("Linked UIK protocols")).toBeInTheDocument();
    expect(screen.getByText(/Party-list · Federal party list/)).toBeInTheDocument();
    expect(screen.getByText(/Single-member · OIK 1 ballot/)).toBeInTheDocument();
    expect(screen.getAllByText(/Candidate Winner · winner/).length).toBeGreaterThan(0);
    expect(screen.getByText("4014005258649")).toBeInTheDocument();
    expect(screen.getByText("Gas vybory id")).toBeInTheDocument();
  });
});
