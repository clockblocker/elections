import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { demoMetadata } from "../demo";
import { DEFAULT_STATE } from "../state";
import { Filters } from "./Filters";

describe("Filters", () => {
  it("exposes keyboard-operable labelled controls", async () => {
    const onChange = vi.fn();
    render(<Filters state={DEFAULT_STATE} metadata={demoMetadata} onChange={onChange} onReset={() => {}} />);
    await userEvent.click(screen.getByRole("checkbox", { name: "Communist Party" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ partyIds: ["united-russia", "communist-party"] }));
    expect(screen.getByLabelText("Minimum turnout")).toHaveValue(0);
    expect(screen.getByRole("combobox", { name: "Region" })).toBeInTheDocument();
  });

  it("restricts candidate choices to the selected district", async () => {
    const onChange = vi.fn();
    const metadata = { ...demoMetadata, districts: [{ id: "77", code: "77-001", name: "77-001 · OIK 1", ballotId: "301" }, { id: "78", code: "78-001", name: "78-001 · OIK 2", ballotId: "302" }], candidates: [{ id: "900", name: "Candidate One", ballotId: "301", districtId: "77", winner: true, position: 1 }, { id: "901", name: "Candidate Two", ballotId: "302", districtId: "78", winner: false, position: 1 }] };
    render(<Filters state={{ ...DEFAULT_STATE, ballotKind: "single_member", ballotId: "301", districtId: "77" }} metadata={metadata} onChange={onChange} onReset={() => {}} />);
    const candidate = screen.getByRole("combobox", { name: "Candidate" });
    expect(candidate).toHaveTextContent("Candidate One");
    expect(candidate).not.toHaveTextContent("Candidate Two");
    await userEvent.selectOptions(candidate, "900");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ candidateId: "900" }));
  });
});
