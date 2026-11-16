import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

const terms = vi.hoisted(() => [] as Array<{ writes: string[]; disposed: boolean }>);

vi.mock("xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    writes: string[] = [];
    disposed = false;
    constructor() {
      terms.push(this);
    }
    loadAddon() {}
    open() {}
    onData() {
      return { dispose() {} };
    }
    write(text: string) {
      if (this.disposed) throw new Error("write after dispose");
      this.writes.push(text);
    }
    dispose() {
      this.disposed = true;
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("xterm/css/xterm.css", () => ({}));
vi.mock("@/lib/api", () => ({ getAccessToken: () => Promise.resolve("tok") }));
const signInWithGitHub = vi.hoisted(() => vi.fn(async () => ({ error: null })));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ signInWithGitHub }) }));

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string, public protocols: string[]) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
}

import InteractiveTerminal from "@/components/InteractiveTerminal";

/** Flush the async token lookup that precedes `new WebSocket`. */
const flush = () => act(async () => {});

describe("InteractiveTerminal", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
    terms.length = 0;
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("sends repo_url as owner/repo", async () => {
    render(<InteractiveTerminal repoUrl="https://github.com/octo/hello" />);
    await flush();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0].url).toBe(
      "ws://localhost:8000/api/terminal/ws?repo_url=octo%2Fhello"
    );
  });

  it("opens one new shell after the repo settles, not one per keystroke", async () => {
    const view = render(<InteractiveTerminal repoUrl="octo/hello" />);
    await flush();
    for (const value of ["octo/hello2", "octo/hello23", "octo/world"]) {
      view.rerender(<InteractiveTerminal repoUrl={value} />);
      await act(async () => {
        vi.advanceTimersByTime(100);
      });
    }
    // Same repository in another format: no reconnect either.
    view.rerender(<InteractiveTerminal repoUrl="https://github.com/octo/world" />);
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    await flush();
    expect(FakeSocket.instances.map((s) => s.url)).toEqual([
      "ws://localhost:8000/api/terminal/ws?repo_url=octo%2Fhello",
      "ws://localhost:8000/api/terminal/ws?repo_url=octo%2Fworld",
    ]);
  });

  it("does not write to the terminal after it is disposed", async () => {
    const view = render(<InteractiveTerminal repoUrl="octo/hello" />);
    await flush();
    const socket = FakeSocket.instances[0];
    const closeHandler = socket.onclose;
    view.unmount();
    // A late close event from the old socket must be a no-op.
    expect(() => closeHandler?.({ code: 1006 })).not.toThrow();
    expect(() => socket.onmessage?.({ data: "late output" })).not.toThrow();
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("reconnects with backoff after an unexpected close, but not after 1008", async () => {
    render(<InteractiveTerminal repoUrl="octo/hello" />);
    await flush();
    act(() => FakeSocket.instances[0].onclose?.({ code: 1006 }));
    await act(async () => {
      vi.advanceTimersByTime(999);
    });
    expect(FakeSocket.instances).toHaveLength(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    await flush();
    expect(FakeSocket.instances).toHaveLength(2);

    act(() => FakeSocket.instances[1].onclose?.({ code: 1008 }));
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await flush();
    expect(FakeSocket.instances).toHaveLength(2);
  });

  /** Close the first socket with `code`, then let any backoff run out. */
  async function closeWith(code: number) {
    const view = render(<InteractiveTerminal repoUrl="octo/hello" />);
    await flush();
    act(() => FakeSocket.instances[0].onclose?.({ code }));
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await flush();
    return view;
  }

  it("does not reconnect after 4401 and offers to sign in again", async () => {
    const view = await closeWith(4401);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(terms[0].writes.join("")).toContain("Sign in again to use the terminal");
    expect(view.queryByRole("button", { name: "Reconnect" })).toBeNull();
    act(() => view.getByRole("button", { name: "Sign in" }).click());
    expect(signInWithGitHub).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect after 4403 and says the terminal is not allowed", async () => {
    const view = await closeWith(4403);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(terms[0].writes.join("")).toContain(
      "Terminal disabled or not allowed for this repository"
    );
    expect(view.getByText("Terminal unavailable")).toBeTruthy();
    // Final: neither an automatic nor an offered retry.
    expect(view.queryByRole("button", { name: "Reconnect" })).toBeNull();
    expect(view.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("keeps reconnecting with backoff after repeated 1006 closes", async () => {
    render(<InteractiveTerminal repoUrl="octo/hello" />);
    await flush();
    for (const [index, delay] of [
      [0, 1000],
      [1, 2000],
      [2, 4000],
    ]) {
      act(() => FakeSocket.instances[index].onclose?.({ code: 1006 }));
      await act(async () => {
        vi.advanceTimersByTime(delay);
      });
      await flush();
      expect(FakeSocket.instances).toHaveLength(index + 2);
    }
  });
});
