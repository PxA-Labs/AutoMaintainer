import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const auth = { user: { id: "u" }, session: { access_token: "t" }, loading: false };
vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));

import AdminPage from "@/app/admin/page";

const metrics = {
  orgs: { total: 1, active: 1, byPlan: { free: 1 } },
  organizations: [],
  runs: { total: 0, running: 0, completed: 0, failed: 0, last24h: 0 },
  usage: {
    totalTokens: 0,
    totalCostCents: 0,
    byEventType: {},
    last30Days: [
      { date: "2026-11-01", tokens: 1_000_000, costCents: 10 },
      { date: "2026-11-02", tokens: 500_000, costCents: 5 },
    ],
  },
  repos: { total: 0, private: null, public: null },
  system: { supabaseHealthy: true, activeConnections: null, queueDepth: null },
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

describe("Admin page", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the dashboard on screen during a background refresh", async () => {
    let metricsCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/admin/metrics")) {
          metricsCalls += 1;
          // First load resolves; the 30s refresh hangs.
          return metricsCalls === 1 ? Promise.resolve(json(metrics)) : new Promise(() => {});
        }
        return new Promise(() => {});
      })
    );

    render(<AdminPage />);
    await act(async () => {});
    expect(screen.getByText("Usage Trend (30 Days)")).toBeTruthy();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    expect(metricsCalls).toBe(2);
    expect(screen.getByText("Usage Trend (30 Days)")).toBeTruthy();
    expect(screen.getByRole("button", { name: /refreshing/i })).toBeTruthy();
  });

  it("scales daily usage bars to the busiest day (no 10000% bars)", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(metrics))));
    render(<AdminPage />);
    await act(async () => {});
    const bars = Array.from(document.querySelectorAll<HTMLElement>("[title$='tokens']"));
    expect(bars.map((b) => b.style.height)).toEqual(["100%", "50%"]);
  });

  it("shows a runs-table error distinctly from an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(url.endsWith("/admin/runs") ? json({ detail: "x" }, 500) : json(metrics))
      )
    );
    render(<AdminPage />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: /agent runs/i }));
    await act(async () => {});
    expect(screen.getByRole("alert").textContent).toContain("Failed to load runs");
    expect(screen.queryByText("No runs found")).toBeNull();
  });
});
