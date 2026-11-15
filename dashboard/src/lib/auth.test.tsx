import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AuthProvider } from "@/lib/auth";

describe("AuthProvider", () => {
  it("renders a configuration error instead of crashing without Supabase env", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "");

    expect(() =>
      render(
        <AuthProvider>
          <div>app</div>
        </AuthProvider>
      )
    ).not.toThrow();
    expect(screen.getByRole("alert").textContent).toContain("NEXT_PUBLIC_SUPABASE_URL");
    expect(screen.queryByText("app")).toBeNull();
  });
});
