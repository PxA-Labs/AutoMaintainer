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

/** Base delay and cap for automatic terminal reconnects. */
export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
/** Give up auto-reconnecting after this many consecutive failures. */
export const RECONNECT_MAX_ATTEMPTS = 6;

/** Exponential backoff: 1s, 2s, 4s, ... capped at RECONNECT_MAX_MS. */
export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_BASE_MS * 2 ** Math.max(0, attempt), RECONNECT_MAX_MS);
}
