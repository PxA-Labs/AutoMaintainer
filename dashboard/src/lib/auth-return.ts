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
  // A path, not "//host" (protocol-relative) or "/\host" (browsers treat the
  // backslash as a slash), and not the callback itself (it would loop).
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return null;
  if (value.startsWith("/auth/")) return null;
  return value;
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
