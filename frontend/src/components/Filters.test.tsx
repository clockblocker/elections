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
});
