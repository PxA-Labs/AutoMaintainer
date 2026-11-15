import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";

// Keyboard/ARIA behaviour of the Web IDE (#407). Native <button>s get Enter /
// Space activation from the browser, which browsers deliver as a click event;
// jsdom does not synthesise it, so the tests assert the role (button), focus
// and the click activation path.

vi.mock("@monaco-editor/react", () => ({
  default: (props: { value?: string }) => <textarea data-testid="editor" readOnly value={props.value ?? ""} />,
}));
vi.mock("./InlineAssist", () => ({ default: () => null }));

type Pending = { url: string; resolve: (r: Response) => void };
const pending: Pending[] = [];

vi.mock("@/lib/api", () => ({
  authedFetch: (url: string) => {
    if (url.endsWith("/tree")) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            name: ".",
            path: ".",
            type: "directory",
            children: [
              {
                name: "src",
                path: "src",
                type: "directory",
                children: [{ name: "app.ts", path: "src/app.ts", type: "file" }],
              },
              { name: "README.md", path: "README.md", type: "file" },
            ],
          })
        )
      );
    }
    return new Promise<Response>((resolve) => pending.push({ url, resolve }));
  },
}));

import WebIDE, { ProposeChangesModal } from "@/components/WebIDE";

const json = (body: unknown) => new Response(JSON.stringify(body));

async function resolvePending(fragment: string, res: Response) {
  const idx = pending.findIndex((p) => p.url.includes(fragment));
  if (idx === -1) throw new Error(`no pending request for ${fragment}`);
  const [p] = pending.splice(idx, 1);
  await act(async () => p.resolve(res));
}

describe("WebIDE keyboard access", () => {
  beforeEach(() => {
    pending.length = 0;
  });

  it("file tree items are buttons with aria-expanded / aria-current", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    const folder = await screen.findByRole("button", { name: "src" });
    expect(folder.getAttribute("aria-expanded")).toBe("false");
    folder.focus();
    fireEvent.click(folder);
    expect(folder.getAttribute("aria-expanded")).toBe("true");

    const file = screen.getByRole("button", { name: "app.ts" });
    fireEvent.click(file);
    await resolvePending("/file?", json({ content: "x" }));
    // The tree entry and the editor tab both mark the open file as current.
    expect(file.getAttribute("aria-current")).toBe("true");
    // No interactive element is nested inside another button.
    expect(file.querySelector("button")).toBeNull();
  });

  it("editor tabs are buttons; activating one switches file and moves aria-current", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    fireEvent.click(await screen.findByRole("button", { name: "README.md" }));
    await resolvePending("README.md", json({ content: "readme" }));
    fireEvent.click(screen.getByRole("button", { name: "src" }));
    fireEvent.click(screen.getByRole("button", { name: "app.ts" }));
    await resolvePending("app.ts", json({ content: "app" }));

    const tabs = within(screen.getByRole("group", { name: "Open files" }));
    const readmeTab = tabs.getByRole("button", { name: "README.md" });
    const appTab = tabs.getByRole("button", { name: "app.ts" });
    expect(appTab.getAttribute("aria-current")).toBe("true");
    expect(readmeTab.getAttribute("aria-current")).toBeNull();

    readmeTab.focus();
    expect(document.activeElement).toBe(readmeTab);
    fireEvent.click(readmeTab);
    expect(readmeTab.getAttribute("aria-current")).toBe("true");
    expect(appTab.getAttribute("aria-current")).toBeNull();
    expect((screen.getByTestId("editor") as HTMLTextAreaElement).value).toBe("readme");
    // Close is a separate, labelled button.
    expect(tabs.getByRole("button", { name: "Close README.md" })).toBeTruthy();
  });

  it("activity bar buttons expose aria-pressed and search results are buttons", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    const explorer = screen.getByRole("button", { name: "Explorer" });
    const search = screen.getByRole("button", { name: "Search" });
    expect(explorer.getAttribute("aria-pressed")).toBe("true");
    expect(search.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(search);
    expect(search.getAttribute("aria-pressed")).toBe("true");
    const box = screen.getByRole("searchbox", { name: /search files/i });
    fireEvent.change(box, { target: { value: "needle" } });
    fireEvent.submit(box);
    await resolvePending("/search?", json({ results: [{ file: "README.md", line_number: 3, snippet: "needle" }] }));

    const result = within(screen.getByRole("list", { name: "Search results" })).getByRole("button");
    expect(result.textContent).toContain("README.md");
    fireEvent.click(result);
    expect(pending.some((p) => p.url.includes("README.md"))).toBe(true);
  });
});

function ModalHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open propose
      </button>
      <ProposeChangesModal
        isOpen={open}
        onClose={() => setOpen(false)}
        changes={[{ path: "a.txt", oldContent: "a", newContent: "b", status: "modified" }]}
        onPropose={() => {}}
        loading={false}
      />
    </>
  );
}

describe("ProposeChangesModal dialog semantics", () => {
  it("is a labelled modal dialog that takes focus, traps Tab, closes on Escape and restores focus", () => {
    render(<ModalHarness />);
    const opener = screen.getByRole("button", { name: "Open propose" });
    opener.focus();
    fireEvent.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Review Changes" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);

    // Tab from the last control wraps to the first; Shift+Tab wraps back.
    const buttons = within(dialog).getAllByRole("button");
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);

    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("associates the PR form labels with their inputs", () => {
    render(<ModalHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Open propose" }));
    fireEvent.click(screen.getByRole("button", { name: /propose changes/i }));

    const dialog = screen.getByRole("dialog", { name: "Create Pull Request" });
    const title = within(dialog).getByLabelText(/pr title/i);
    expect(title.tagName).toBe("INPUT");
    expect(document.activeElement).toBe(title);
    expect(within(dialog).getByLabelText(/description/i).tagName).toBe("TEXTAREA");
  });
});
