import { expect, test } from "@playwright/test";
import { E2E_ORIGIN, E2E_SUPABASE_URL } from "./env";
import { API, isolateNetwork, json, mockSupabaseLogs, recordRequests, signIn } from "./mocks";

const RUN_ID = "11111111-2222-4333-8444-555555555555";

test.beforeEach(async ({ context }) => {
  await isolateNetwork(context);
});

test.describe("signed out", () => {
  test("renders the auth gate", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "AutoMaintainer" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in with GitHub" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Skip for now" })).toBeVisible();
  });

  test("guest view shows the dashboard and asks to sign in before a run", async ({ page }) => {
    const starts = recordRequests(page, (r) => r.url() === API.start);
    await page.goto("/");
    await page.getByRole("button", { name: "Skip for now" }).click();

    await expect(page.getByRole("navigation", { name: "Main Navigation" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByText("System halted")).toBeVisible();

    // A guest cannot start a run: the auth gate comes back, nothing is sent.
    await page.getByRole("button", { name: "Start agent loop" }).click();
    await expect(page.getByRole("button", { name: "Sign in with GitHub" })).toBeVisible();
    expect(starts).toHaveLength(0);
  });

  test("sign-in sends GitHub OAuth back to /auth/callback/", async ({ page, context }) => {
    let authorizeUrl: URL | null = null;
    await context.route(`${E2E_SUPABASE_URL}/auth/v1/authorize**`, (route) => {
      authorizeUrl = new URL(route.request().url());
      return route.fulfill({ contentType: "text/html", body: "<p>GitHub OAuth (mocked)</p>" });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Sign in with GitHub" }).click();
    await expect(page.getByText("GitHub OAuth (mocked)")).toBeVisible();

    expect(authorizeUrl).not.toBeNull();
    const url = authorizeUrl as unknown as URL;
    expect(url.searchParams.get("provider")).toBe("github");
    expect(url.searchParams.get("redirect_to")).toBe(`${E2E_ORIGIN}/auth/callback/`);
  });
});

test.describe("signed in", () => {
  test("starting a run shows the running state and polls its status", async ({ page, context }) => {
    const token = await signIn(page);
    const queriedLogs = await mockSupabaseLogs(context, (runId) => [
      {
        id: "log-1",
        run_id: runId,
        agent_name: "Architect",
        message: "Planning the fix for acme/widgets",
        log_type: "log",
        created_at: "2026-11-01T10:00:00Z",
      },
    ]);

    let startBody: unknown = null;
    let startAuth: string | null = null;
    await context.route(API.start, (route) => {
      startBody = route.request().postDataJSON();
      startAuth = route.request().headers()["authorization"] ?? null;
      return json(route, { run_id: RUN_ID, status: "queued" });
    });
    // The run reports "running" until the test marks it finished.
    let finished = false;
    let statusCalls = 0;
    await context.route(API.status(RUN_ID), (route) => {
      statusCalls += 1;
      const status = finished ? "completed" : "running";
      return json(route, { run_id: RUN_ID, status, repo_name: "acme/widgets" });
    });

    await page.goto("/");
    await page.getByRole("button", { name: /Repository Identifier owner\/repo/ }).click();
    const repo = page.getByRole("textbox", { name: "Repository Identifier" });
    await expect(repo).toBeFocused();
    await repo.fill("acme/widgets");
    await repo.press("Enter");

    await page.getByRole("button", { name: "Start agent loop" }).click();

    // Running: the control flips to Stop, the header reports activity and
    // the run id is kept in the URL so a reload re-attaches.
    await expect(page.getByRole("button", { name: "Stop agent loop" })).toBeVisible();
    await expect(page.getByText("System active")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`[?&]run=${RUN_ID}`));
    expect(startBody).toEqual({ repo_name: "acme/widgets", target_issue: null });
    expect(startAuth).toBe(`Bearer ${token}`);

    // Live logs for the new run are loaded from Supabase.
    await expect(page.getByText("Planning the fix for acme/widgets")).toBeVisible();
    expect(queriedLogs).toContain(RUN_ID);

    // Status is polled immediately; once the run finishes server-side, the
    // next interval poll (every 5 s) notices and resets the UI.
    expect(statusCalls).toBeGreaterThanOrEqual(1);
    const callsWhileRunning = statusCalls;
    finished = true;
    await expect.poll(() => statusCalls, { timeout: 15_000 }).toBeGreaterThan(callsWhileRunning);
    await expect(page.getByRole("button", { name: "Start agent loop" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("System halted")).toBeVisible();
    await expect(page).not.toHaveURL(/[?&]run=/);
  });

  test("run history is keyboard navigable", async ({ page, context }) => {
    await signIn(page);
    const queriedLogs = await mockSupabaseLogs(context);
    const runs = [
      {
        id: "aaaaaaaa-0000-4000-8000-000000000001",
        repo_name: "acme/widgets",
        status: "completed",
        created_at: "2026-11-01T10:00:00Z",
        pr_url: "https://github.com/acme/widgets/pull/7",
      },
      {
        id: "bbbbbbbb-0000-4000-8000-000000000002",
        repo_name: "acme/gadgets",
        status: "failed",
        created_at: "2026-11-02T10:00:00Z",
        error_message: "Tests failed",
      },
    ];
    await context.route(API.runs, (route) => json(route, { runs }));

    await page.goto("/");
    const historyTab = page.getByRole("button", { name: "Run History" });
    await historyTab.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Execution Run History" })).toBeVisible();

    // Tab order: Refresh -> run 1 -> its PR link -> run 2.
    await page.getByRole("button", { name: "Refresh run history" }).focus();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: /View logs for run aaaaaaaa/ })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "View Generated Pull Request" })).toBeFocused();
    await page.keyboard.press("Tab");
    const second = page.getByRole("button", { name: /View logs for run bbbbbbbb \(failed, acme\/gadgets\)/ });
    await expect(second).toBeFocused();

    // Enter opens that run's logs on the dashboard.
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    await expect.poll(() => queriedLogs).toContain(runs[1].id);

    // And the history marks it as the current run.
    await page.getByRole("button", { name: "Run History" }).click();
    await expect(second).toHaveAttribute("aria-current", "true");
  });

  test("admin page renders the metrics it is given", async ({ page, context }) => {
    await signIn(page);
    await context.route(API.adminMetrics, (route) =>
      json(route, {
        orgs: { total: 3, active: 2, byPlan: { free: 2, pro: 1 } },
        organizations: [
          {
            org_id: "org-1",
            org_name: "Acme Corp",
            slug: "acme",
            plan: "pro",
            member_count: 4,
            repo_count: 2,
            active_runs: 0,
            runs_24h: 5,
            failed_24h: 1,
            estimated_cost_30d_usd: 12.34,
            last_run_at: null,
          },
        ],
        runs: { total: 10, running: 1, completed: 7, failed: 2, cancelled: 0, last24h: 6 },
        usage: {
          totalTokens: 1_500_000,
          totalCostCents: 1234,
          byEventType: { agent_run: { count: 10, costCents: 1234 } },
          last30Days: [{ date: "2026-11-01", tokens: 1000, costCents: 10 }],
        },
        repos: { total: 4, private: 3, public: 1 },
        system: { supabaseHealthy: true, activeConnections: null, queueDepth: 2 },
      })
    );

    await page.goto("/admin/");
    await expect(page.getByRole("heading", { name: "Admin Dashboard" })).toBeVisible();
    await expect(page.getByText("Supabase: Healthy")).toBeVisible();
    await expect(page.getByText("Queue: 2")).toBeVisible();
    await expect(page.getByText("2 active")).toBeVisible();
    await expect(page.getByText("$12.34")).toBeVisible();
    await expect(page.getByText("1.5M tokens")).toBeVisible();

    await page.getByRole("button", { name: "Organizations" }).click();
    await expect(page.getByRole("cell", { name: /Acme Corp/ })).toBeVisible();
    await expect(page.getByText("Organizations (3)")).toBeVisible();
  });
});
