import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// Monaco is replaced by a textarea that exposes the same value/onChange contract.
vi.mock("@monaco-editor/react", () => ({
  default: (props: { value?: string; onChange?: (v: string) => void }) => (
    <textarea
      data-testid="editor"
      value={props.value ?? ""}
      onChange={(e) => props.onChange?.(e.target.value)}
    />
  ),
}));
vi.mock("./InlineAssist", () => ({ default: () => null }));

type Pending = { url: string; init?: RequestInit; resolve: (r: Response) => void };
const pending: Pending[] = [];
const calls: Array<{ url: string; init?: RequestInit }> = [];
// What GET /repo/{name}/working-changes answers (the caller's branch diff).
const working: { body: unknown } = { body: { branch: null, changes: [] } };

vi.mock("@/lib/api", () => ({
  authedFetch: (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/working-changes")) {
      return Promise.resolve(new Response(JSON.stringify(working.body)));
    }
    if (url.endsWith("/tree")) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            name: ".",
            path: ".",
            type: "directory",
            children: [{ name: "README.md", path: "README.md", type: "file" }],
          })
        )
      );
    }
    return new Promise<Response>((resolve) => pending.push({ url, init, resolve }));
  },
}));

import WebIDE from "@/components/WebIDE";

const fileJson = (content: string) => new Response(JSON.stringify({ content }));

function resolveFile(repo: string, res: Response) {
  const idx = pending.findIndex((p) => p.url.startsWith(`/repo/${encodeURIComponent(repo)}/file`));
  if (idx === -1) throw new Error(`no pending file request for ${repo}`);
  const [p] = pending.splice(idx, 1);
  p.resolve(res);
}

const fileGets = () => calls.filter((c) => c.url.includes("/file?") && !c.init?.method);

// The explorer entry precedes the tab label of the same name in the DOM.
const treeEntry = () => screen.getAllByText("README.md")[0];

async function openReadme() {
  await screen.findAllByText("README.md");
  fireEvent.click(treeEntry());
}

describe("WebIDE", () => {
  beforeEach(() => {
    pending.length = 0;
    calls.length = 0;
    working.body = { branch: null, changes: [] };
  });

  it("restores the staged list from the working branch after a reload (#505)", async () => {
    working.body = {
      branch: "automaintainer/ide-abc123",
      changes: [
        { path: "README.md", status: "modified" },
        { path: "gone.py", status: "deleted" },
      ],
    };
    render(<WebIDE repoUrl="octo/a" />);

    // No tab is open: the sidebar's Propose button appears once restored.
    fireEvent.click(await screen.findByRole("button", { name: /^propose changes$/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("gone.py");
    fireEvent.click(within(dialog).getByRole("button", { name: /^propose changes$/i }));
    fireEvent.change(await screen.findByLabelText(/pr title/i), { target: { value: "Restore" } });
    fireEvent.click(screen.getByRole("button", { name: /create pull request/i }));

    // The restored file's content comes from the caller's own view.
    await waitFor(() => expect(fileGets()).toHaveLength(1));
    expect(fileGets()[0].url).toContain("file_path=README.md");
    await act(async () => resolveFile("octo/a", fileJson("saved on my branch")));

    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/propose-changes"))).toBe(true));
    const propose = calls.find((c) => c.url.endsWith("/propose-changes"));
    expect(JSON.parse(String(propose!.init?.body)).changes).toEqual([
      { path: "README.md", content: "saved on my branch", status: "modified" },
      { path: "gone.py", content: "", status: "deleted" },
    ]);
  });

  it("keeps a restored file staged when it is edited", async () => {
    working.body = { branch: "automaintainer/ide-abc123", changes: [{ path: "README.md", status: "modified" }] };
    render(<WebIDE repoUrl="octo/a" />);
    await screen.findByRole("button", { name: /^propose changes$/i });
    await openReadme();
    await act(async () => resolveFile("octo/a", fileJson("saved")));
    expect((await screen.findByRole("status")).textContent).toContain("automaintainer/ide-abc123");
    fireEvent.change(await screen.findByTestId("editor"), { target: { value: "edited" } });
    fireEvent.change(await screen.findByTestId("editor"), { target: { value: "saved" } });

    // Back to the loaded content, but its default-branch baseline is
    // unknown, so it stays staged.
    expect(screen.getByRole("button", { name: /^propose \(1\)$/i })).toBeDefined();
  });

  it("ignores a malformed working-changes answer", async () => {
    working.body = { changes: "nope" };
    render(<WebIDE repoUrl="octo/a" />);
    await screen.findAllByText("README.md");
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/working-changes"))).toBe(true));
    expect(screen.queryByRole("button", { name: /^propose/i })).toBeNull();
  });

  it("does not let a slow response from the previous repo fill the new repo's tab", async () => {
    const view = render(<WebIDE repoUrl="octo/a" />);
    await openReadme(); // request for octo/a is held

    view.rerender(<WebIDE repoUrl="octo/b" />);
    await openReadme();
    await act(async () => resolveFile("octo/b", fileJson("content of B")));
    await act(async () => resolveFile("octo/a", fileJson("content of A")));

    expect((await screen.findByTestId("editor")) as HTMLTextAreaElement).toHaveProperty(
      "value",
      "content of B"
    );
  });

  it("shows a read-only error and never saves after a failed load", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    await openReadme();
    await act(async () => resolveFile("octo/a", new Response("", { status: 503 })));

    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      expect.stringContaining("Could not load README.md")
    );
    expect(screen.queryByTestId("editor")).toBeNull();
    const save = screen.getByRole("button", { name: /save/i }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);
    expect(calls.some((c) => c.init?.method === "POST")).toBe(false);

    // Retry loads the real content.
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await act(async () => resolveFile("octo/a", fileJson("real")));
    expect(await screen.findByTestId("editor")).toHaveProperty("value", "real");
  });

  it("dedupes a double-click and keeps edits typed after the first load", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    await openReadme();
    fireEvent.click(treeEntry());
    expect(fileGets()).toHaveLength(1);

    await act(async () => resolveFile("octo/a", fileJson("v1")));
    const editor = await screen.findByTestId("editor");
    fireEvent.change(editor, { target: { value: "my edit" } });
    fireEvent.click(treeEntry());

    await waitFor(() => expect(screen.getByTestId("editor")).toHaveProperty("value", "my edit"));
    expect(fileGets()).toHaveLength(1);
  });

  it("keeps a saved file staged, shows its working branch and proposes it (#139)", async () => {
    render(<WebIDE repoUrl="octo/a" />);
    await openReadme();
    await act(async () => resolveFile("octo/a", fileJson("v1")));
    fireEvent.change(await screen.findByTestId("editor"), { target: { value: "v2" } });

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    const save = pending.find((p) => p.init?.method === "POST");
    expect(save).toBeDefined();
    pending.splice(pending.indexOf(save!), 1);
    await act(async () =>
      save!.resolve(
        new Response(JSON.stringify({ status: "updated", branch: "automaintainer/ide-abc123" }))
      )
    );

    expect((await screen.findByRole("status")).textContent).toContain("automaintainer/ide-abc123");
    fireEvent.click(screen.getByRole("button", { name: /^propose \(1\)$/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^propose changes$/i }));
    fireEvent.change(await screen.findByLabelText(/pr title/i), { target: { value: "Edit readme" } });
    fireEvent.click(screen.getByRole("button", { name: /create pull request/i }));

    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/propose-changes"))).toBe(true));
    const propose = calls.find((c) => c.url.endsWith("/propose-changes"));
    expect(JSON.parse(String(propose!.init?.body)).changes).toEqual([
      { path: "README.md", content: "v2", status: "modified" },
    ]);
  });

  it("reports unsaved edits to the parent", async () => {
    const onDirtyChange = vi.fn();
    render(<WebIDE repoUrl="octo/a" onDirtyChange={onDirtyChange} />);
    await openReadme();
    await act(async () => resolveFile("octo/a", fileJson("v1")));
    fireEvent.change(await screen.findByTestId("editor"), { target: { value: "v2" } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
  });
});
