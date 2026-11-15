import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { UserMenu } from "@/components/ui/UserMenu";

describe("UserMenu", () => {
  it("includes the visible user name in the button's accessible name", () => {
    render(<UserMenu user={{ user_metadata: { full_name: "Octo Cat" } }} onSignOut={() => {}} />);
    // WCAG 2.5.3 (label in name): the name starts with the visible text.
    expect(screen.getByRole("button", { name: "Octo Cat, account menu" })).toBeTruthy();
  });

  it("falls back to the email, then a generic label", () => {
    const { rerender } = render(<UserMenu user={{ email: "octo@example.test" }} onSignOut={() => {}} />);
    expect(screen.getByRole("button", { name: "octo@example.test, account menu" })).toBeTruthy();
    rerender(<UserMenu user={null} onSignOut={() => {}} />);
    expect(screen.getByRole("button", { name: "User, account menu" })).toBeTruthy();
  });
});
