import { defineConfig, devices } from "@playwright/test";
import { E2E_ORIGIN, E2E_PORT, E2E_SUPABASE_URL } from "./e2e/env";

/**
 * Smoke tests against the real static export (`next build`, output: export)
 * served from out/. No backend, Supabase or GitHub is contacted: every
 * request leaving the page is either answered by a page.route mock or
 * aborted (see e2e/mocks.ts).
 *
 * The build is made with test-only public env: the backend URL is the
 * static server's own origin (as in production, where FastAPI serves the
 * export) so API calls are same-origin and mocked by path, and Supabase
 * points at a reserved .invalid host that can never resolve.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  // One retry in CI so a single infrastructure hiccup does not fail the PR;
  // the HTML report flags anything that only passed on retry as flaky.
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [["github"], ["list"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: E2E_ORIGIN,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Optional local escape hatch (e.g. E2E_BROWSER_CHANNEL=chrome) to
        // run against an installed Chrome/Edge when the Playwright browser
        // download is unavailable. CI leaves it unset and uses the pinned
        // Playwright Chromium.
        channel: process.env.E2E_BROWSER_CHANNEL || undefined,
      },
    },
  ],
  webServer: {
    // E2E_SKIP_BUILD=1 reuses an existing e2e build in out/ when iterating
    // locally; CI always builds.
    command: process.env.E2E_SKIP_BUILD
      ? "node e2e/serve.mjs"
      : "npm run build && node e2e/serve.mjs",
    url: E2E_ORIGIN,
    reuseExistingServer: !process.env.CI,
    timeout: 300_000,
    stdout: "pipe",
    env: {
      E2E_PORT: String(E2E_PORT),
      NEXT_PUBLIC_BACKEND_URL: E2E_ORIGIN,
      NEXT_PUBLIC_SUPABASE_URL: E2E_SUPABASE_URL,
      NEXT_PUBLIC_SUPABASE_ANON_KEY: "e2e-anon-key",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  },
});
