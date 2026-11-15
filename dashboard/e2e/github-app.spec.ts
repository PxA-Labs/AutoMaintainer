import { expect, test } from "@playwright/test";
import { expectNoA11yViolations } from "./axe";
import { E2E_ORIGIN } from "./env";
import { API, isolateNetwork, json, mockGitHubAppTables, recordRequests, signIn, type GitHubAppTables } from "./mocks";

// GitHub App install flow (#470): dashboard -> POST /github/app/install-url
// -> GitHub -> GET /github/app/setup -> dashboard ?github_setup=<outcome>.
// GitHub is mocked as a redirect back to the dashboard so the browser
// round trip runs without contacting anything real.

const INSTALL_URL = "https://github.com/apps/auto-maintainer/installations/new?state=signed-state";
const GITHUB_TOKEN = "gho_e2e_user_token";

const LINKED: GitHubAppTables = {
  role: "owner",
  installations: [{ id: 42, account_login: "acme", suspended_at: null }],
  repositories: [
    { id: 1, full_name: "acme/widgets", github_installation_id: 42 },
    { id: 2, full_name: "acme/gadgets", github_installation_id: 42 },
  ],
};

test.beforeEach(async ({ context }) => {
  await isolateNetwork(context);
});

/**
 * GitHub's install page answers with the redirect the backend setup route
 * ends in: back to the dashboard with `dashboardQuery`. The intermediate
 * hop through GET /github/app/setup is collapsed because Playwright does
 * not intercept the follow-up request of a redirect it fulfilled; the setup
 * route itself is covered by the backend tests. Returns the URLs visited.
 */
async function mockGitHubRoundTrip(
  context: import("@playwright/test").BrowserContext,
  dashboardQuery: string,
  onSetup: () => void = () => {}
) {
  const visited: string[] = [];
  await context.route("https://github.com/apps/**", (route) => {
    visited.push(route.request().url());
    onSetup();
    return route.fulfill({ status: 302, headers: { Location: `${E2E_ORIGIN}/?${dashboardQuery}` } });
  });
  return visited;
}

test("an admin installs the App and comes back to a linked installation", async ({ page, context }) => {
  const token = await signIn(page, { providerToken: GITHUB_TOKEN });
  // Nothing is linked until the setup route has run.
  let linked = false;
  const counts = await mockGitHubAppTables(context, () => (linked ? LINKED : { role: "owner" }));

  let installHeaders: Record<string, string> = {};
  await context.route(API.githubInstallUrl, (route) => {
    installHeaders = route.request().headers();
    expect(route.request().method()).toBe("POST");
    return json(route, { install_url: INSTALL_URL, expires_in: 600 });
  });
  const visited = await mockGitHubRoundTrip(context, "github_setup=linked&github_installation_id=42", () => {
    linked = true;
  });

  await page.goto("/");
  await expect(page.getByText("No GitHub App installation is linked to your organization.")).toBeVisible();
  const queriesBefore = counts.installations;

  await page.getByRole("button", { name: "Install GitHub App" }).click();

  // The request carried both credentials the route requires.
  await expect(page.getByRole("status").filter({ hasText: "GitHub App installed" })).toBeVisible();
  expect(installHeaders["authorization"]).toBe(`Bearer ${token}`);
  expect(installHeaders["x-github-user-token"]).toBe(GITHUB_TOKEN);
  expect(visited).toEqual([INSTALL_URL]);

  // Back on the dashboard: the outcome is announced, the parameters are
  // gone from the URL and the lists were reloaded with the new installation.
  await expect(page.getByRole("status").filter({ hasText: "installation 42" })).toBeVisible();
  await expect(page).toHaveURL(`${E2E_ORIGIN}/`);
  const installations = page.getByRole("list", { name: "Linked GitHub App installations" });
  await expect(installations).toContainText("acme");
  await expect(page.getByText("2 repositories available")).toBeVisible();
  expect(counts.installations).toBeGreaterThan(queriesBefore);
  await expectNoA11yViolations(page, "GitHub App setup success");
});

test("a failed setup redirect shows the reason as an alert", async ({ page, context }) => {
  await signIn(page, { providerToken: GITHUB_TOKEN });
  await mockGitHubAppTables(context, { role: "admin" });
  await context.route(API.githubInstallUrl, (route) => json(route, { install_url: INSTALL_URL }));
  await mockGitHubRoundTrip(context, "github_setup=expired");

  await page.goto("/");
  await page.getByRole("button", { name: "Install GitHub App" }).click();

  const alert = page.getByRole("alert").filter({ hasText: "GitHub App not linked" });
  await expect(alert).toContainText("The install link expired");
  await expect(page).not.toHaveURL(/github_setup/);
  await expect(page.getByText("No GitHub App installation is linked to your organization.")).toBeVisible();
  await expectNoA11yViolations(page, "GitHub App setup failure");

  await page.getByRole("button", { name: "Dismiss GitHub App setup message" }).click();
  await expect(alert).toHaveCount(0);
});

test("members do not get the install action, and an install error is announced", async ({ page, context }) => {
  await signIn(page, { providerToken: GITHUB_TOKEN });
  let role = "member";
  await mockGitHubAppTables(context, () => ({ ...LINKED, role }));
  const installCalls = recordRequests(page, (r) => r.url() === API.githubInstallUrl);
  await context.route(API.githubInstallUrl, (route) =>
    json(route, { detail: "GitHub App install flow is not configured" }, 503)
  );

  await page.goto("/");
  await expect(page.getByRole("list", { name: "Linked GitHub App installations" })).toContainText("acme");
  await expect(page.getByRole("button", { name: "Install GitHub App" })).toHaveCount(0);

  // Promoted to admin: after a refresh the action appears; the server-side
  // failure is reported without leaving the page.
  role = "admin";
  await page.getByRole("button", { name: "Refresh GitHub App installations" }).click();
  await page.getByRole("button", { name: "Install GitHub App" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "not configured on the server" })).toBeVisible();
  expect(installCalls).toHaveLength(1);
  await expect(page).toHaveURL(`${E2E_ORIGIN}/`);
});
