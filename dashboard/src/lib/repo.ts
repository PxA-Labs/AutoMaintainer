/**
 * Repository identifier helpers.
 *
 * The sidebar accepts either "owner/repo" or a GitHub URL. Backend endpoints
 * (notably the terminal websocket's repo_url) expect the canonical
 * "owner/repo" slug, so user input is normalised through here.
 */

const SEGMENT = "[A-Za-z0-9_.-]+";
const SLUG_RE = new RegExp(`^(${SEGMENT})/(${SEGMENT})$`);
const URL_RE = new RegExp(
  `^(?:https?://)?(?:www\\.)?github\\.com/(${SEGMENT})/(${SEGMENT})/?$`,
  "i"
);

/**
 * Normalise a user-entered repository to "owner/repo".
 * Returns null when the value is empty, the "owner/repo" placeholder, or not
 * recognisable as a GitHub repository.
 */
export function normalizeRepoSlug(input: string | null | undefined): string | null {
  if (!input) return null;
  const value = input.trim();
  if (!value || value === "owner/repo") return null;

  const match = SLUG_RE.exec(value) ?? URL_RE.exec(value);
  if (!match) return null;

  const owner = match[1];
  const repo = match[2].replace(/\.git$/i, "");
  if (!repo || [owner, repo].some((s) => s === "." || s === "..")) {
    return null;
  }
  return `${owner}/${repo}`;
}
