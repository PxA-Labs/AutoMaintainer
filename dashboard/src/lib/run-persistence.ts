/**
 * Remembers the active agent run across page reloads.
 *
 * The run id lives in the `?run=<id>` query parameter so a reload (or a copied
 * link) lands back on the same run. sessionStorage is the fallback for when the
 * parameter has been dropped, e.g. after navigating to /admin and back within
 * the same tab. sessionStorage rather than localStorage: a second tab must not
 * silently attach itself to a run that a different tab started.
 *
 * The URL is updated with history.replaceState instead of the Next router. The
 * dashboard is also served as a static export, and the router would require a
 * Suspense boundary around useSearchParams for no benefit here; Next keeps its
 * own router state in sync with replaceState calls.
 */

export const RUN_QUERY_PARAM = "run";
export const RUN_STORAGE_KEY = "automaintainer.activeRunId";

// Run ids are UUIDs; anything else in the URL is ignored rather than sent to
// the backend.
const RUN_ID_PATTERN = /^[A-Za-z0-9-]{1,64}$/;

function isValidRunId(value: string | null | undefined): value is string {
  return !!value && RUN_ID_PATTERN.test(value);
}

function readStorage(): string | null {
  try {
    return window.sessionStorage.getItem(RUN_STORAGE_KEY);
  } catch {
    // Storage can be unavailable (privacy mode, sandboxed iframes).
    return null;
  }
}

function writeStorage(value: string | null): void {
  try {
    if (value) window.sessionStorage.setItem(RUN_STORAGE_KEY, value);
    else window.sessionStorage.removeItem(RUN_STORAGE_KEY);
  } catch {
    // Best effort only: the URL parameter still carries the id.
  }
}

function writeQueryParam(value: string | null): void {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set(RUN_QUERY_PARAM, value);
  else url.searchParams.delete(RUN_QUERY_PARAM);
  if (url.href !== window.location.href) {
    window.history.replaceState(null, "", url.href);
  }
}

/** The persisted active run id: the URL parameter wins over sessionStorage. */
export function readPersistedRunId(): string | null {
  if (typeof window === "undefined") return null;
  const fromUrl = new URL(window.location.href).searchParams.get(RUN_QUERY_PARAM);
  if (isValidRunId(fromUrl)) return fromUrl;
  const fromStorage = readStorage();
  return isValidRunId(fromStorage) ? fromStorage : null;
}

/** Record `runId` as the active run in both the URL and sessionStorage. */
export function persistRunId(runId: string): void {
  if (typeof window === "undefined" || !isValidRunId(runId)) return;
  writeQueryParam(runId);
  writeStorage(runId);
}

/** Forget the active run (it finished, was stopped, or no longer exists). */
export function clearPersistedRunId(): void {
  if (typeof window === "undefined") return;
  writeQueryParam(null);
  writeStorage(null);
}
