import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

/* eslint-disable @typescript-eslint/no-explicit-any */

// Fake Supabase client: history query resolves empty, and the realtime
// postgres_changes callback is captured so the test can push rows.
const realtime: { push?: (row: any) => void } = {};
const client = {
  from: () => ({
    select: () => ({
      eq: () => ({
        order: () => Promise.resolve({ data: [], error: null }),
      }),
    }),
  }),
  channel: () => {
    const ch: any = {
      on: (_type: string, _filter: unknown, cb: (p: any) => void) => {
        realtime.push = (row) => cb({ new: row });
        return ch;
      },
      subscribe: () => ch,
    };
    return ch;
  },
  removeChannel: vi.fn(),
};
vi.mock("@/lib/supabase", () => ({ getBrowserClient: () => client }));

import { useLogs } from "@/lib/hooks/use-logs";
import { MAX_ACTIVITY_ENTRIES, MAX_LOG_ENTRIES } from "@/lib/logs";

describe("useLogs", () => {
  it("caps realtime logs and activity", async () => {
    const { result } = renderHook(() => useLogs("run-1"));
    await waitFor(() => expect(realtime.push).toBeDefined());
    // Let the (empty) history replay finish so rows are applied, not buffered.
    await act(async () => {});

    const total = MAX_LOG_ENTRIES + 500;
    await act(async () => {
      for (let i = 0; i < total; i++) {
        realtime.push!({ id: `l${i}`, log_type: "log", message: `line ${i}` });
      }
      for (let i = 0; i < MAX_ACTIVITY_ENTRIES + 10; i++) {
        realtime.push!({ id: `a${i}`, log_type: "ui_update", metadata: { activity: { id: i } } });
      }
    });

    expect(result.current.logs).toHaveLength(MAX_LOG_ENTRIES);
    expect(result.current.logs.at(-1)?.msg).toBe(`line ${total - 1}`);
    expect(result.current.activity).toHaveLength(MAX_ACTIVITY_ENTRIES);
  });

  it("caps locally added logs", () => {
    const { result } = renderHook(() => useLogs(null));
    act(() => {
      for (let i = 0; i < MAX_LOG_ENTRIES + 5; i++) {
        result.current.addLog({ time: "", agent: "System", msg: String(i), color: "" });
      }
    });
    expect(result.current.logs).toHaveLength(MAX_LOG_ENTRIES);
  });
});
