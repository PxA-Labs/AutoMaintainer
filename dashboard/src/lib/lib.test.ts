import { describe, expect, it } from "vitest";
import { appendCapped, prependCapped, MAX_LOG_ENTRIES } from "@/lib/logs";
import { normalizeRepoSlug } from "@/lib/repo";
import {
  buildTerminalWsUrl,
  classifyTerminalClose,
  reconnectDelay,
  RECONNECT_MAX_MS,
} from "@/lib/terminal";
import { isTerminalRunStatus } from "@/lib/run-status";
import { barPercent } from "@/lib/chart";
import { readOAuthError } from "@/lib/oauth";

describe("log capping", () => {
  it("keeps only the newest entries", () => {
    const prev = Array.from({ length: MAX_LOG_ENTRIES }, (_, i) => i);
    const next = appendCapped(prev, [MAX_LOG_ENTRIES, MAX_LOG_ENTRIES + 1]);
    expect(next).toHaveLength(MAX_LOG_ENTRIES);
    expect(next[0]).toBe(2);
    expect(next.at(-1)).toBe(MAX_LOG_ENTRIES + 1);
  });

  it("returns the same array when nothing is appended", () => {
    const prev = [1, 2];
    expect(appendCapped(prev, [])).toBe(prev);
  });

  it("prepends newest-first with a cap", () => {
    expect(prependCapped([2, 1], 3, 2)).toEqual([3, 2]);
  });
});

describe("normalizeRepoSlug", () => {
  it.each([
    ["octo/hello", "octo/hello"],
    ["  octo/hello  ", "octo/hello"],
    ["https://github.com/octo/hello", "octo/hello"],
    ["https://github.com/octo/hello.git", "octo/hello"],
    ["github.com/octo/hello/", "octo/hello"],
  ])("%s -> %s", (input, expected) => {
    expect(normalizeRepoSlug(input)).toBe(expected);
  });

  it.each(["", "owner/repo", "octo", "octo/hello/extra", "../etc", "https://evil.com/octo/hello", "./x"])(
    "rejects %j",
    (input) => {
      expect(normalizeRepoSlug(input)).toBeNull();
    }
  );
});

describe("terminal websocket url", () => {
  it("sends the repository as an owner/repo slug", () => {
    expect(buildTerminalWsUrl("http://localhost:8000", "https://github.com/octo/hello")).toBe(
      "ws://localhost:8000/api/terminal/ws?repo_url=octo%2Fhello"
    );
  });

  it("uses wss for https backends and omits invalid repos", () => {
    expect(buildTerminalWsUrl("https://api.example.com/", "owner/repo")).toBe(
      "wss://api.example.com/api/terminal/ws"
    );
  });

  it("backs off exponentially with a cap", () => {
    expect(reconnectDelay(0)).toBe(1000);
    expect(reconnectDelay(3)).toBe(8000);
    expect(reconnectDelay(20)).toBe(RECONNECT_MAX_MS);
  });

  it.each([
    [4401, "signin"],
    [4403, "forbidden"],
    [1008, "policy"],
    [1006, "retry"],
    [1000, "retry"],
    [1011, "retry"],
  ] as const)("classifies close code %i as %s", (code, action) => {
    expect(classifyTerminalClose(code)).toBe(action);
  });
});

describe("run status", () => {
  it("treats only queued/running as active", () => {
    expect(isTerminalRunStatus("queued")).toBe(false);
    expect(isTerminalRunStatus("running")).toBe(false);
    expect(isTerminalRunStatus("completed")).toBe(true);
    expect(isTerminalRunStatus("failed")).toBe(true);
    expect(isTerminalRunStatus("cancelled")).toBe(true);
    expect(isTerminalRunStatus(undefined)).toBe(false);
  });
});

describe("barPercent", () => {
  it("never yields NaN or Infinity and clamps to 100", () => {
    expect(barPercent(5, 0)).toBe(0);
    expect(barPercent(0, 0)).toBe(0);
    expect(barPercent(1_000_000, 10_000)).toBe(100);
    expect(barPercent(25, 100)).toBe(25);
  });
});

describe("readOAuthError", () => {
  it("reads errors from the query string or the hash", () => {
    expect(readOAuthError("https://x.test/auth/callback/?error=access_denied&error_description=User+denied")).toBe(
      "User denied"
    );
    expect(readOAuthError("https://x.test/auth/callback/#error=server_error")).toBe("server_error");
    expect(readOAuthError("https://x.test/auth/callback/?code=abc")).toBeNull();
  });
});
