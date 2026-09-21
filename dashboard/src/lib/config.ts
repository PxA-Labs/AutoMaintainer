/**
 * Shared configuration utilities.
 * Deduplicates getBackendUrl() which was previously copy-pasted
 * across page.tsx, WebIDE.tsx, InteractiveTerminal.tsx, and admin/page.tsx.
 */

/**
 * Resolves the backend base URL.
 * Priority order:
 *  1. NEXT_PUBLIC_BACKEND_URL env var (e.g. for custom cloud deployments)
 *  2. In local dev (Next.js runs on :3000, FastAPI on :8000) -> use localhost:8000
 *  3. In production (static export served from FastAPI itself) -> use same host
 */
export function getBackendUrl(): string {
  if (process.env.NEXT_PUBLIC_BACKEND_URL) {
    return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    const port = window.location.port;
    const hostname = window.location.hostname;
    // In local dev (Next.js can run on 3000, 3001, etc. but FastAPI runs on 8000)
    if (
      (hostname === "localhost" ||
        hostname === "127.0.0.1" ||
        hostname === "::1" ||
        hostname === "[::1]") &&
      port !== "8000"
    ) {
      const formattedHost =
        hostname === "::1" || hostname === "[::1]" ? "[::1]" : hostname;
      return `${window.location.protocol}//${formattedHost}:8000`;
    }
    // Otherwise (production / Docker), same host serves both
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://localhost:8000";
}

/**
 * Parse a target issue string into a number or null.
 * Accepts formats like "123", "#123", or empty string (null).
 */
export function parseTargetIssue(value: string): number | null {
  const normalized = value.trim().replace(/^#/, "");
  if (!normalized) return null;
  if (!/^\d+$/.test(normalized)) {
    throw new Error(
      "Target issue must be a positive integer, for example 123 or #123."
    );
  }

  const issueNumber = Number(normalized);
  if (!Number.isSafeInteger(issueNumber) || issueNumber < 1) {
    throw new Error(
      "Target issue must be a positive integer within the safe number range."
    );
  }
  return issueNumber;
}
