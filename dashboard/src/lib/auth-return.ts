/**
 * Where to go back to after signing in.
 *
 * GitHub OAuth leaves the page and comes back to /auth/callback/, which used
 * to always continue to "/". Before leaving, the current location (path, query
 * such as ?run=, and hash) is kept in sessionStorage, which survives the
 * redirect in the same tab, and the callback page returns there.
 *
 * Only a same-origin path is ever stored or returned: the value is read back
 * from storage, so it is checked again rather than trusted (no open redirect).
 */

export const RETURN_TO_STORAGE_KEY = "automaintainer.returnTo";
const FALLBACK = "/";
const MAX_LENGTH = 2048;

/** `value` if it is a safe same-origin path to return to, else null. */
export function safeReturnPath(value: unknown): string | null {
  if (typeof value !== "string" || value.length > MAX_LENGTH) return null;
  // Control characters are never part of a location we stored, and the URL
  // parser silently strips tabs and newlines ("/\t/evil" becomes "//evil").
  if (/[\u0000-\u001f\u007f]/.test(value)) return null;
  // A path, not "//host" (protocol-relative) or "/\host" (browsers treat the
  // backslash as a slash).
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return null;
  // Resolve it the way navigation will, and keep only a same-origin result,
  // re-serialised from its parts so nothing the parser reinterpreted remains.
  const base = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  let url: URL;
  try {
    url = new URL(value, base);
  } catch {
    return null;
  }
  if (url.origin !== new URL(base).origin) return null;
  // Not the callback itself (it would loop).
  if (url.pathname.startsWith("/auth/")) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Remember the current location so the sign-in callback can return to it. */
export function rememberReturnTo(): void {
  if (typeof window === "undefined") return;
  const { pathname, search, hash } = window.location;
  const path = safeReturnPath(`${pathname}${search}${hash}`);
  try {
    if (path) window.sessionStorage.setItem(RETURN_TO_STORAGE_KEY, path);
    else window.sessionStorage.removeItem(RETURN_TO_STORAGE_KEY);
  } catch {
    // Storage unavailable (privacy mode): the callback falls back to "/".
  }
}

/** The remembered location (or "/"), forgotten once read. */
export function consumeReturnTo(): string {
  if (typeof window === "undefined") return FALLBACK;
  try {
    const stored = window.sessionStorage.getItem(RETURN_TO_STORAGE_KEY);
    window.sessionStorage.removeItem(RETURN_TO_STORAGE_KEY);
    return safeReturnPath(stored) ?? FALLBACK;
  } catch {
    return FALLBACK;
  }
}
