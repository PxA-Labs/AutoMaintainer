import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

// A stable auth object: a new object per render would re-create callbacks.
const auth = { user: { id: "u" }, session: { access_token: "t" } };
vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));

import { useAgentRun, RUN_STATUS_POLL_MS } from "@/lib/hooks/use-agent-run";
import { RUN_STORAGE_KEY } from "@/lib/run-persistence";

type Call = { url: string; init?: RequestInit };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** fetch stub whose responses are resolved manually, per URL suffix. */
function deferredFetch() {
  const calls: Call[] = [];
  const pending: Array<{ url: string; resolve: (r: Response) => void }> = [];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Promise<Response>((resolve) => pending.push({ url, resolve }));
  });
  const resolve = (suffix: string, res: Response) => {
    const idx = pending.findIndex((p) => p.url.includes(suffix));
    if (idx === -1) throw new Error(`no pending request for ${suffix}`);
    const [p] = pending.splice(idx, 1);
    p.resolve(res);
  };
  return { calls, fetchMock, resolve };
}

function setup() {
  const opts = {};
  const onLog = vi.fn();
  const hook = renderHook(() => useAgentRun(onLog, opts));
  act(() => hook.result.current.actions.setRepoUrl("octo/hello"));
  return { hook, onLog };
}

describe("useAgentRun", () => {
  let f: ReturnType<typeof deferredFetch>;

  beforeEach(() => {
    // A run started by a previous test is persisted; start each test fresh.
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    f = deferredFetch();
    vi.stubGlobal("fetch", f.fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("ignores a second Start while the first is in flight", async () => {
    const { hook } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
      void hook.result.current.actions.startStop();
    });
    expect(f.calls.filter((c) => c.url.endsWith("/start"))).toHaveLength(1);
    expect(hook.result.current.state.phase).toBe("starting");
    expect(hook.result.current.state.isPending).toBe(true);
  });

  it("never sends a body-less /stop while /start is pending", async () => {
    const { hook } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    expect(f.calls.some((c) => c.url.endsWith("/stop"))).toBe(false);
  });

  it("stays running when /stop fails, and stops only after success", async () => {
    const { hook } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => f.resolve("/start", jsonResponse({ run_id: "run-12345678" })));
    await waitFor(() => expect(hook.result.current.state.phase).toBe("running"));
    // Initial status poll: still running.
    await act(async () => f.resolve("/status/", jsonResponse({ status: "running" })));

    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    expect(hook.result.current.state.phase).toBe("stopping");
    const stop = f.calls.find((c) => c.url.endsWith("/stop"));
    expect(JSON.parse(String(stop?.init?.body))).toEqual({ run_id: "run-12345678" });

    await act(async () => f.resolve("/stop", jsonResponse({ detail: "boom" }, 503)));
    await waitFor(() => expect(hook.result.current.state.phase).toBe("running"));
    expect(hook.result.current.state.isRunning).toBe(true);

    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => f.resolve("/stop", jsonResponse({ status: "cancelled" })));
    await waitFor(() => expect(hook.result.current.state.phase).toBe("idle"));
  });

  it("returns to idle when the backend reports the run finished", async () => {
    vi.useFakeTimers();
    const { hook, onLog } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => f.resolve("/start", jsonResponse({ run_id: "run-abcdefgh" })));
    expect(hook.result.current.state.phase).toBe("running");

    await act(async () => f.resolve("/status/", jsonResponse({ status: "running" })));
    expect(hook.result.current.state.isRunning).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(RUN_STATUS_POLL_MS);
    });
    await act(async () => f.resolve("/status/", jsonResponse({ status: "failed" })));
    expect(hook.result.current.state.phase).toBe("idle");
    expect(hook.result.current.state.isRunning).toBe(false);
    expect(onLog.mock.calls.some(([e]) => String(e.msg).includes("failed"))).toBe(true);
  });

  it("returns to idle when /start fails", async () => {
    const { hook } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => f.resolve("/start", jsonResponse({ detail: "nope" }, 500)));
    await waitFor(() => expect(hook.result.current.state.phase).toBe("idle"));
    expect(hook.result.current.state.activeRunId).toBeNull();
  });
});

describe("useAgentRun run restore after reload (#400)", () => {
  let f: ReturnType<typeof deferredFetch>;
  const runParam = () => new URL(window.location.href).searchParams.get("run");

  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    f = deferredFetch();
    vi.stubGlobal("fetch", f.fetchMock);
  });

  it("persists the run id in ?run= and sessionStorage once a run starts", async () => {
    const { hook } = setup();
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    await act(async () => f.resolve("/start", jsonResponse({ run_id: "run-1111" })));
    expect(runParam()).toBe("run-1111");
    expect(window.sessionStorage.getItem(RUN_STORAGE_KEY)).toBe("run-1111");
  });

  it("resumes a running run from ?run= on load: Running state, repo and Stop work", async () => {
    window.history.replaceState(null, "", "/?run=run-2222");
    const onLog = vi.fn();
    const hook = renderHook(() => useAgentRun(onLog, {}));

    // While GET /status is in flight Start is blocked (no second run).
    expect(hook.result.current.state.phase).toBe("restoring");
    expect(hook.result.current.state.isPending).toBe(true);
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    expect(f.calls.some((c) => c.url.endsWith("/start"))).toBe(false);
    expect(f.calls[0].url).toMatch(/\/status\/run-2222$/);

    await act(async () =>
      f.resolve("/status/", jsonResponse({ status: "running", repo_name: "octo/live" }))
    );
    expect(hook.result.current.state.phase).toBe("running");
    expect(hook.result.current.state.isRunning).toBe(true);
    expect(hook.result.current.state.activeRunId).toBe("run-2222");
    expect(hook.result.current.state.repoUrl).toBe("octo/live");

    // Stop targets the restored run, and a successful stop forgets it.
    await act(async () => f.resolve("/status/", jsonResponse({ status: "running" })));
    await act(async () => {
      void hook.result.current.actions.startStop();
    });
    const stop = f.calls.find((c) => c.url.endsWith("/stop"));
    expect(JSON.parse(String(stop?.init?.body))).toEqual({ run_id: "run-2222" });
    await act(async () => f.resolve("/stop", jsonResponse({ status: "cancelled" })));
    expect(hook.result.current.state.phase).toBe("idle");
    expect(runParam()).toBeNull();
    expect(window.sessionStorage.getItem(RUN_STORAGE_KEY)).toBeNull();
  });

  it("falls back to sessionStorage when the URL has no ?run=", async () => {
    window.sessionStorage.setItem(RUN_STORAGE_KEY, "run-3333");
    const hook = renderHook(() => useAgentRun(vi.fn(), {}));
    expect(f.calls[0].url).toMatch(/\/status\/run-3333$/);
    await act(async () => f.resolve("/status/", jsonResponse({ status: "queued" })));
    expect(hook.result.current.state.phase).toBe("running");
    expect(runParam()).toBe("run-3333");
  });

  it.each([
    ["finished", jsonResponse({ status: "completed" })],
    ["unknown", jsonResponse({ detail: "Run not found" }, 404)],
  ])("forgets a %s run and stays idle", async (_label, response) => {
    window.history.replaceState(null, "", "/?run=run-4444&tab=x");
    const hook = renderHook(() => useAgentRun(vi.fn(), {}));
    await act(async () => f.resolve("/status/", response));
    expect(hook.result.current.state.phase).toBe("idle");
    expect(hook.result.current.state.activeRunId).toBeNull();
    expect(runParam()).toBeNull();
    // Unrelated query parameters are preserved.
    expect(new URL(window.location.href).searchParams.get("tab")).toBe("x");
  });

  it("keeps the remembered run when the status check fails transiently", async () => {
    window.history.replaceState(null, "", "/?run=run-5555");
    const hook = renderHook(() => useAgentRun(vi.fn(), {}));
    await act(async () => f.resolve("/status/", jsonResponse({ detail: "db down" }, 503)));
    expect(hook.result.current.state.phase).toBe("idle");
    expect(runParam()).toBe("run-5555");
  });

  it("ignores a malformed ?run= value", () => {
    window.history.replaceState(null, "", "/?run=..%2F..%2Fadmin");
    const hook = renderHook(() => useAgentRun(vi.fn(), {}));
    expect(f.calls).toHaveLength(0);
    expect(hook.result.current.state.phase).toBe("idle");
  });

  it("resumeRun() attaches to an offered run", async () => {
    const hook = renderHook(() => useAgentRun(vi.fn(), {}));
    let resumed: Promise<boolean> = Promise.resolve(false);
    await act(async () => {
      resumed = hook.result.current.actions.resumeRun("run-6666");
    });
    await act(async () => f.resolve("/status/", jsonResponse({ status: "running" })));
    await expect(resumed).resolves.toBe(true);
    expect(hook.result.current.state.activeRunId).toBe("run-6666");
    expect(runParam()).toBe("run-6666");
  });
});
