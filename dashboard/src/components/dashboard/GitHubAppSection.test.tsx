import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { GitHubAppSection } from "./GitHubAppSection";
import { GitHubSetupBanner } from "./GitHubSetupBanner";
import { readSetupResult } from "@/lib/github-app";

function props(overrides: Partial<Parameters<typeof GitHubAppSection>[0]> = {}) {
  return {
    state: {
      role: "owner",
      installations: [{ id: 42, account_login: "acme", suspended_at: null }],
      repositories: [
        { id: 1, full_name: "acme/a", github_installation_id: 42 },
        { id: 2, full_name: "acme/b", github_installation_id: 42 },
      ],
    },
    loading: false,
    loadError: null,
    isAdmin: true,
    installing: false,
    installError: null,
    install: vi.fn(async () => {}),
    refresh: vi.fn(async () => {}),
    ...overrides,
  };
}

describe("GitHubAppSection", () => {
  it("lists installations and the repository count", () => {
    render(<GitHubAppSection {...props()} />);
    const list = screen.getByRole("list", { name: "Linked GitHub App installations" });
    expect(list.textContent).toContain("acme");
    expect(screen.getByText("2 repositories available")).toBeTruthy();
  });

  it("offers the install action to admins and calls it", () => {
    const install = vi.fn(async () => {});
    render(<GitHubAppSection {...props({ install })} />);
    fireEvent.click(screen.getByRole("button", { name: "Install GitHub App" }));
    expect(install).toHaveBeenCalledTimes(1);
  });

  it("hides the install action from members", () => {
    render(<GitHubAppSection {...props({ isAdmin: false })} />);
    expect(screen.queryByRole("button", { name: "Install GitHub App" })).toBeNull();
  });

  it("shows install and load errors as alerts", () => {
    const { unmount } = render(<GitHubAppSection {...props({ installError: "Sign in again" })} />);
    expect(screen.getByRole("alert").textContent).toBe("Sign in again");
    unmount();
    render(<GitHubAppSection {...props({ state: null, loadError: "permission denied" })} />);
    expect(screen.getByRole("alert").textContent).toContain("permission denied");
  });

  it("disables the action while it navigates to GitHub", () => {
    render(<GitHubAppSection {...props({ installing: true })} />);
    const button = screen.getByRole("button", { name: "Opening GitHub..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("says when nothing is linked", () => {
    render(<GitHubAppSection {...props({ state: { role: "member", installations: [], repositories: [] } })} />);
    expect(screen.getByText("No GitHub App installation is linked to your organization.")).toBeTruthy();
  });
});

describe("GitHubSetupBanner", () => {
  it("announces success politely", () => {
    render(<GitHubSetupBanner result={readSetupResult("?github_setup=linked&github_installation_id=42")!} onDismiss={() => {}} />);
    expect(screen.getByRole("status").textContent).toContain("installation 42");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("announces a failure as an alert with its reason, and can be dismissed", () => {
    const onDismiss = vi.fn();
    render(<GitHubSetupBanner result={readSetupResult("?github_setup=expired")!} onDismiss={onDismiss} />);
    expect(screen.getByRole("alert").textContent).toMatch(/not linked.*expired/);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss GitHub App setup message" }));
    expect(onDismiss).toHaveBeenCalled();
  });
});
