import { expect, test } from "@playwright/test";
import { expectNoA11yViolations } from "./axe";
import { E2E_ORIGIN } from "./env";
import { isolateNetwork, json, serveMonacoFromNodeModules, signIn } from "./mocks";

// Accessibility audit of the IDE tab (#471): file tree, search view, Monaco
// editor and the inline assist panel, with the repository API mocked.

const REPO = "acme/widgets";

const TREE = {
  name: "widgets",
  path: ".",
  type: "directory",
  children: [
    {
      name: "src",
      path: "src",
      type: "directory",
      children: [{ name: "index.ts", path: "src/index.ts", type: "file" }],
    },
    { name: "README.md", path: "README.md", type: "file" },
  ],
};

const FILES: Record<string, string> = {
  "src/index.ts": 'export function greet(name: string) {\n  return `Hello, ${name}!`;\n}\n',
  "README.md": "# widgets\n",
};

test.beforeEach(async ({ context }) => {
  await isolateNetwork(context);
  await serveMonacoFromNodeModules(context);
  const prefix = `/repo/${encodeURIComponent(REPO)}/`;
  await context.route(`${E2E_ORIGIN}/repo/**`, (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith(prefix)) return json(route, { detail: "Not found" }, 404);
    const rest = url.pathname.slice(prefix.length);
    if (rest === "tree") return json(route, TREE);
    if (rest === "file" && route.request().method() === "GET") {
      const path = url.searchParams.get("file_path") ?? "";
      return path in FILES ? json(route, { content: FILES[path] }) : json(route, { detail: "Not found" }, 404);
    }
    if (rest === "search") return json(route, { results: [] });
    return json(route, { detail: "Not mocked" }, 404);
  });
});

test("the IDE tab has no serious accessibility violations", async ({ page }) => {
  // Four axe scans plus loading Monaco (several MB of AMD modules) exceed
  // the default 30 s on a loaded runner; each step still has its own
  // 10 s expect timeout, so a real hang fails quickly.
  test.setTimeout(90_000);
  await signIn(page);
  await page.goto("/");

  await page.getByRole("button", { name: /Repository Identifier owner\/repo/ }).click();
  const repo = page.getByRole("textbox", { name: "Repository Identifier" });
  await repo.fill(REPO);
  await repo.press("Enter");
  await page.getByRole("button", { name: "Web IDE" }).click();

  // File tree with nothing open.
  const src = page.getByRole("button", { name: "src", exact: true });
  await expect(src).toBeVisible();
  await expect(page.getByText("Select a file from the explorer or search to begin.")).toBeVisible();
  await expectNoA11yViolations(page, "IDE file tree");

  // Search view, before a query.
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByText("Enter a query to search.")).toBeVisible();
  await expectNoA11yViolations(page, "IDE search");
  await page.getByRole("button", { name: "Explorer", exact: true }).click();

  // Editor with a file open (Monaco served from node_modules).
  await page.getByRole("button", { name: "src", exact: true }).click();
  await page.getByRole("button", { name: "index.ts", exact: true }).click();
  await expect(page.locator(".monaco-editor .view-lines")).toContainText("Hello");
  await expectNoA11yViolations(page, "IDE editor");

  // Inline assist panel.
  await page.getByRole("button", { name: /Inline Assist/ }).click();
  const assist = page.getByRole("dialog", { name: "Inline Assist" });
  await expect(assist).toBeVisible();
  await expect(assist.getByRole("textbox", { name: "Inline Assist instruction" })).toBeVisible();
  await expectNoA11yViolations(page, "IDE inline assist");
});
