/**
 * Extract an OAuth error from a callback URL. Supabase reports provider
 * errors as `error`/`error_description` in either the query string (PKCE)
 * or the hash fragment (implicit flow).
 */
export function readOAuthError(href: string): string | null {
  const url = new URL(href);
  const hash = new URLSearchParams(url.hash.replace(/^#/, ""));
  for (const params of [url.searchParams, hash]) {
    const description = params.get("error_description") ?? params.get("error");
    if (description) return description;
  }
  return null;
}
