import { readFile } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import type { BrowserContext, Page, Request, Route } from "@playwright/test";
import { E2E_ORIGIN, E2E_SUPABASE_URL, SUPABASE_STORAGE_KEY } from "./env";

/** Same-origin backend endpoints the dashboard calls (see getBackendUrl). */
export const API = {
  runs: `${E2E_ORIGIN}/runs`,
  start: `${E2E_ORIGIN}/start`,
  status: (runId: string) => `${E2E_ORIGIN}/status/${runId}`,
  health: `${E2E_ORIGIN}/healthz/supabase`,
  adminMetrics: `${E2E_ORIGIN}/admin/metrics`,
  adminRuns: `${E2E_ORIGIN}/admin/runs`,
  githubInstallUrl: `${E2E_ORIGIN}/github/app/install-url`,
  repo: (repo: string, rest: string) => `${E2E_ORIGIN}/repo/${encodeURIComponent(repo)}/${rest}`,
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": E2E_ORIGIN,
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
};

export function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: CORS_HEADERS,
    body: JSON.stringify(body),
  });
}

/**
 * Fence the browser context off from the network. Registered first, so the
 * more specific routes added later take precedence (Playwright matches the
 * most recently added route first). Anything left unmocked that is not the
 * static export itself is aborted, and the Supabase Realtime websocket is
 * accepted but never connected to a server.
 */
export async function isolateNetwork(context: BrowserContext) {
  await context.route(
    (url) => url.origin !== E2E_ORIGIN,
    (route) => route.abort("blockedbyclient")
  );
  await context.routeWebSocket(/\/realtime\/v1\/websocket/, () => {
    // Intentionally empty: no connectToServer(), so nothing leaves the page.
  });
  // Every backend call is mocked per test; default to an empty run list
  // and a healthy Supabase so the shell renders without error banners.
  await context.route(API.health, (route) => json(route, { status: "ok" }));
  await context.route(API.runs, (route) => json(route, { runs: [] }));
  // The GitHub App section reads these under RLS; default to "no org".
  await mockGitHubAppTables(context, { role: null });
}

export interface GitHubAppTables {
  /** Org role of the signed-in user; null means no accepted membership. */
  role: string | null;
  installations?: { id: number; account_login: string; suspended_at: string | null }[];
  repositories?: { id: number; full_name: string; github_installation_id: number }[];
}

/**
 * Mock the PostgREST queries of fetchGitHubAppState() (lib/github-app.ts).
 * `tables` may be a function so a test can change the data mid-test (for
 * example after the setup redirect has linked an installation). Returns the
 * number of installation-list queries made so far.
 */
export async function mockGitHubAppTables(
  context: BrowserContext,
  tables: GitHubAppTables | (() => GitHubAppTables)
) {
  const current = () => (typeof tables === "function" ? tables() : tables);
  const counts = { installations: 0 };
  const table = (name: string, rows: () => unknown[]) =>
    context.route(`${E2E_SUPABASE_URL}/rest/v1/${name}?**`, (route) => {
      if (route.request().method() === "OPTIONS") {
        return route.fulfill({ status: 204, headers: CORS_HEADERS });
      }
      if (name === "github_installations") counts.installations += 1;
      return json(route, rows());
    });
  await table("organization_members", () => {
    const { role } = current();
    return role ? [{ org_id: "org-1", role }] : [];
  });
  await table("github_installations", () => current().installations ?? []);
  await table("repositories", () => current().repositories ?? []);
  return counts;
}

// Monaco is loaded by @monaco-editor/loader from jsdelivr at runtime. The
// network is fenced off, so serve the same files from the locally installed
// monaco-editor package (any version: only the editor's own DOM is needed).
const MONACO_VS = join(__dirname, "..", "node_modules", "monaco-editor", "min", "vs");
const MONACO_TYPES: Record<string, string> = {
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".ttf": "font/ttf",
  ".json": "application/json; charset=utf-8",
};

export async function serveMonacoFromNodeModules(context: BrowserContext) {
  await context.route(/^https:\/\/cdn\.jsdelivr\.net\/npm\/monaco-editor@[^/]+\/min\/vs\/(.+)$/, async (route) => {
    const rel = decodeURIComponent(new URL(route.request().url()).pathname.replace(/^.*\/min\/vs\//, ""));
    const file = normalize(join(MONACO_VS, rel));
    if (!file.startsWith(MONACO_VS + sep)) return route.abort("blockedbyclient");
    try {
      const body = await readFile(file);
      return route.fulfill({
        status: 200,
        headers: { "Access-Control-Allow-Origin": "*" },
        contentType: MONACO_TYPES[extname(file)] ?? "application/octet-stream",
        body,
      });
    } catch {
      return route.fulfill({ status: 404, body: "" });
    }
  });
}

export interface LogRow {
  id: string;
  run_id: string;
  agent_name: string;
  message: string;
  log_type: string;
  created_at: string;
}

/**
 * Mock the PostgREST `logs` query useLogs() makes for the selected run.
 * Returns the run ids that were queried, in order.
 */
export async function mockSupabaseLogs(
  context: BrowserContext,
  rowsFor: (runId: string) => LogRow[] = () => []
) {
  const queried: string[] = [];
  await context.route(`${E2E_SUPABASE_URL}/rest/v1/logs**`, (route) => {
    if (route.request().method() === "OPTIONS") {
      return route.fulfill({ status: 204, headers: CORS_HEADERS });
    }
    const runId = new URL(route.request().url()).searchParams.get("run_id")?.replace(/^eq\./, "") ?? "";
    queried.push(runId);
    return json(route, rowsFor(runId));
  });
  return queried;
}

function base64url(value: object) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

export const TEST_USER = {
  id: "00000000-0000-4000-8000-000000000001",
  aud: "authenticated",
  role: "authenticated",
  email: "octocat@example.test",
  app_metadata: { provider: "github", providers: ["github"] },
  user_metadata: { user_name: "octocat", full_name: "Octo Cat" },
  created_at: "2026-01-01T00:00:00Z",
};

/**
 * Seed a signed-in Supabase session in localStorage before the app boots.
 * supabase-js trusts a stored session until it is near expiry, so with an
 * expiry an hour out no token refresh (network call) is attempted. The
 * token is a well-formed but unsigned JWT; nothing verifies it here.
 */
export async function signIn(page: Page, { providerToken }: { providerToken?: string } = {}) {
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  const accessToken = [
    base64url({ alg: "HS256", typ: "JWT" }),
    base64url({ sub: TEST_USER.id, role: "authenticated", exp: expiresAt }),
    "e2e-signature",
  ].join(".");
  const session = {
    access_token: accessToken,
    refresh_token: "e2e-refresh-token",
    token_type: "bearer",
    expires_in: 3600,
    expires_at: expiresAt,
    user: TEST_USER,
    // The GitHub OAuth token Supabase keeps after a GitHub sign-in; the
    // install flow sends it as X-GitHub-User-Token.
    ...(providerToken ? { provider_token: providerToken } : {}),
  };
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [SUPABASE_STORAGE_KEY, JSON.stringify(session)] as const
  );
  return accessToken;
}

/** Collect requests matching `predicate` for later assertions. */
export function recordRequests(page: Page, predicate: (request: Request) => boolean) {
  const seen: Request[] = [];
  page.on("request", (request) => {
    if (predicate(request)) seen.push(request);
  });
  return seen;
}
