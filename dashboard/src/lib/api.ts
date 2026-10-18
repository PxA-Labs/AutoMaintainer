/**
 * Authenticated access to the backend API.
 *
 * Every /repo/... route and /assist/inline requires a Supabase bearer token.
 * The token is read from the Supabase client rather than React context so that
 * deeply nested components (WebIDE, InlineAssist) do not have to thread an
 * auth prop through every layer.
 */

import { getBackendUrl } from "@/lib/config";
import { getBrowserClient } from "@/lib/supabase";

/** Thrown when the backend rejects the request for lack of valid credentials. */
export class UnauthenticatedError extends Error {
  constructor(message = "Your session has expired. Please sign in again.") {
    super(message);
    this.name = "UnauthenticatedError";
  }
}

export async function getAccessToken(): Promise<string | null> {
  try {
    const { data } = await getBrowserClient().auth.getSession();
    return data.session?.access_token ?? null;
  } catch {
    return null;
  }
}

/**
 * fetch() against the backend with the caller's bearer token attached.
 *
 * `path` may be absolute or a backend-relative path such as "/repo/x/tree".
 * A 401 is surfaced as UnauthenticatedError so callers can prompt a re-login
 * instead of rendering a generic failure.
 */
export async function authedFetch(
  path: string,
  init: RequestInit = {}
): Promise<Response> {
  const token = await getAccessToken();
  if (!token) {
    throw new UnauthenticatedError("You must be signed in to do that.");
  }

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);

  const url = /^https?:\/\//.test(path) ? path : `${getBackendUrl()}${path}`;
  const response = await fetch(url, { ...init, headers });

  if (response.status === 401) {
    throw new UnauthenticatedError();
  }
  return response;
}
