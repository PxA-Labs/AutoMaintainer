import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RunHistory } from "@/components/dashboard/RunHistory";
import { ResumeRunBanner } from "@/components/dashboard/ResumeRunBanner";

const runs = [
  {
    id: "aaaaaaaa-1111",
    status: "running",
    repo_name: "octo/a",
    created_at: "2026-01-01T00:00:00Z",
    pr_url: "https://github.com/octo/a/pull/1",
  },
  { id: "bbbbbbbb-2222", status: "failed", repo_name: "octo/b", created_at: "2026-01-01T00:00:00Z" },
];

describe("RunHistory accessibility (#407)", () => {
  it("exposes each run as a focusable button that selects it", () => {
    const onSelectRun = vi.fn();
    render(
      <RunHistory runs={runs} loading={false} onRefresh={() => {}} onSelectRun={onSelectRun} currentRunId="bbbbbbbb-2222" />
    );

    const first = screen.getByRole("button", { name: /view logs for run aaaaaaaa \(running, octo\/a\)/i });
    // A native <button>: reachable with Tab, activated by Enter/Space (which
    // browsers dispatch as click; jsdom does not synthesise that, so click).
    expect(first.tagName).toBe("BUTTON");
    first.focus();
    expect(document.activeElement).toBe(first);
    fireEvent.click(first);
    expect(onSelectRun).toHaveBeenCalledWith("aaaaaaaa-1111");

    const current = screen.getByRole("button", { name: /bbbbbbbb/ });
    expect(current.getAttribute("aria-current")).toBe("true");
    expect(first.getAttribute("aria-current")).toBeNull();
  });

  it("keeps the PR link a separate control that does not select the run", () => {
    const onSelectRun = vi.fn();
    render(<RunHistory runs={runs} loading={false} onRefresh={() => {}} onSelectRun={onSelectRun} />);
    const link = screen.getByRole("link", { name: /view generated pull request/i });
    expect(link.closest("button")).toBeNull();
    fireEvent.click(link);
    expect(onSelectRun).not.toHaveBeenCalled();
  });

  it("renders the run list as a list", () => {
    render(<RunHistory runs={runs} loading={false} onRefresh={() => {}} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    // Without a selection handler there is nothing to activate.
    expect(screen.queryByRole("button", { name: /view logs/i })).toBeNull();
  });
});

describe("ResumeRunBanner (#400)", () => {
  it("offers to resume or dismiss the active run", () => {
    const onResume = vi.fn();
    const onDismiss = vi.fn();
    render(
      <ResumeRunBanner runId="cccccccc-3333" repoName="octo/a" status="running" onResume={onResume} onDismiss={onDismiss} />
    );
    expect(screen.getByRole("status").textContent).toContain("cccccccc");
    fireEvent.click(screen.getByRole("button", { name: /resume tracking/i }));
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(onResume).toHaveBeenCalledOnce();
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
