import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

let sessionResult: { data: { session: unknown }; error: { message: string } | null };
vi.mock("@/lib/supabase", () => ({
  getBrowserClient: () => ({
    auth: {
      getSession: () => Promise.resolve(sessionResult),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
    },
  }),
}));

import AuthCallbackPage from "@/app/auth/callback/page";

describe("auth callback page", () => {
  beforeEach(() => {
    replace.mockReset();
    window.history.replaceState(null, "", "/auth/callback/");
  });

  it("shows a loading state, then goes to the dashboard once the session exists", async () => {
    sessionResult = { data: { session: { access_token: "t" } }, error: null };
    render(<AuthCallbackPage />);
    expect(screen.getByText(/completing sign-in/i)).toBeTruthy();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("shows the provider error when sign-in failed", async () => {
    window.history.replaceState(
      null,
      "",
      "/auth/callback/?error=access_denied&error_description=User+denied+access"
    );
    sessionResult = { data: { session: null }, error: null };
    render(<AuthCallbackPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("User denied access");
    expect(replace).not.toHaveBeenCalled();
  });
});
