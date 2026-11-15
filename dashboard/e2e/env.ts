/** Shared constants for the Playwright config and the smoke tests. */

export const E2E_PORT = Number(process.env.E2E_PORT || 4173);

/** The static export server; also the (mocked) backend origin. */
export const E2E_ORIGIN = `http://127.0.0.1:${E2E_PORT}`;

/**
 * Supabase URL baked into the e2e build. `.invalid` is reserved (RFC 2606),
 * so even an unmocked request can never reach a real host.
 */
export const E2E_SUPABASE_URL = "http://supabase.e2e.invalid";

/** supabase-js stores the session under sb-<first host label>-auth-token. */
export const SUPABASE_STORAGE_KEY = "sb-supabase-auth-token";
