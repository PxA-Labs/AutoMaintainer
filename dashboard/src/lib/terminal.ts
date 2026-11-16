import { normalizeRepoSlug } from "@/lib/repo";

/**
 * Build the terminal websocket URL for a backend base URL.
 *
 * The repository is sent as the canonical "owner/repo" slug (the backend
 * resolves it to the cloned workspace); anything that does not normalise to a
 * slug is omitted rather than sent through verbatim.
 */
export function buildTerminalWsUrl(backendUrl: string, repo?: string | null): string {
  const protocol = backendUrl.startsWith("https") ? "wss:" : "ws:";
  const host = backendUrl.replace(/^https?:\/\//, "").replace(/\/$/, "");
  let url = `${protocol}//${host}/api/terminal/ws`;
  const slug = normalizeRepoSlug(repo);
  if (slug) {
    url += `?repo_url=${encodeURIComponent(slug)}`;
  }
  return url;
}

/**
 * Close codes the backend's terminal WebSocket refuses a handshake with
 * (backend/main.py), mirroring HTTP 401 and 403. 1008 (policy violation) is
 * what older backends sent for every refusal.
 */
export const WS_CLOSE_POLICY_VIOLATION = 1008;
export const WS_CLOSE_UNAUTHENTICATED = 4401;
export const WS_CLOSE_FORBIDDEN = 4403;

/**
 * What a terminal close means for the client:
 * - "signin": the token is missing, expired or not an org member's; only
 *   signing in again can help.
 * - "forbidden": the terminal is disabled, the user is not allowed to use it,
 *   or the repository is not the user's organization's. Final.
 * - "policy": the legacy 1008 refusal. Final.
 * - "retry": anything else (1006 abnormal close, server restart, ...); worth
 *   reconnecting with backoff. A refused Origin also arrives as 1006, because
 *   the backend closes before the handshake completes.
 */
export type TerminalCloseAction = "signin" | "forbidden" | "policy" | "retry";

export function classifyTerminalClose(code: number): TerminalCloseAction {
  switch (code) {
    case WS_CLOSE_UNAUTHENTICATED:
      return "signin";
    case WS_CLOSE_FORBIDDEN:
      return "forbidden";
    case WS_CLOSE_POLICY_VIOLATION:
      return "policy";
    default:
      return "retry";
  }
}

/** Base delay and cap for automatic terminal reconnects. */
export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
/** Give up auto-reconnecting after this many consecutive failures. */
export const RECONNECT_MAX_ATTEMPTS = 6;

/** Exponential backoff: 1s, 2s, 4s, ... capped at RECONNECT_MAX_MS. */
export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_BASE_MS * 2 ** Math.max(0, attempt), RECONNECT_MAX_MS);
}
