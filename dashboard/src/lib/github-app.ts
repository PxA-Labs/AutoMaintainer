/**
 * GitHub App install flow (backend: POST /github/app/install-url and
 * GET /github/app/setup, see docs/DEPLOYMENT.md "Install flow").
 *
 * 1. An org owner/admin asks the backend for an install URL. The request
 *    carries only the Supabase bearer token (no GitHub token: Supabase drops
 *    `provider_token` when it refreshes the session, #481). The backend
 *    binds a signed, expiring, single-use `state` to (user, org, the user's
 *    GitHub identity in Supabase Auth) and returns the GitHub install URL.
 * 2. After the install, GitHub calls the backend's setup route, which
 *    verifies everything (including the installer's own GitHub OAuth code,
 *    exchanged server-side) and redirects back to the dashboard with
 *    `github_setup=<outcome>` and, when linked, `github_installation_id`.
 * 3. The dashboard reads (and then removes) those parameters, reports the
 *    outcome and reloads the org's installations and repositories.
 */

import { authedFetch, UnauthenticatedError } from "@/lib/api";
import { getBrowserClient } from "@/lib/supabase";

/** Org roles allowed to install the App (backend ADMIN_ROLES). */
export const ADMIN_ROLES: ReadonlySet<string> = new Set(["owner", "admin"]);

export function isOrgAdmin(role: string | null | undefined): boolean {
  return !!role && ADMIN_ROLES.has(role);
}

/** Query parameters the setup route appends to the dashboard URL. */
export const SETUP_PARAM = "github_setup";
export const INSTALLATION_PARAM = "github_installation_id";

export interface SetupResult {
  outcome: string;
  /** "success" is announced politely (role=status); "error" as an alert. */
  tone: "success" | "info" | "error";
  message: string;
}

// One message per outcome the backend can send (main._complete_app_setup).
const OUTCOME_MESSAGES: Record<string, { tone: SetupResult["tone"]; message: string }> = {
  requested: {
    tone: "info",
    message:
      "Installation requested. An owner of the GitHub account must approve it; then start the install again from here to link it.",
  },
  unverified: {
    tone: "error",
    message:
      "The installation was not linked because it could not be verified. Start the install from the dashboard's Install GitHub App button.",
  },
  invalid: {
    tone: "error",
    message: "GitHub sent back an invalid setup request, so nothing was linked. Start the install again.",
  },
  expired: {
    tone: "error",
    message:
      "The install link expired (it is valid for 10 minutes and can be used once), so nothing was linked. Start the install again.",
  },
  forbidden: {
    tone: "error",
    message:
      "The installation was not linked: you must be an owner or admin of this organization, and your GitHub account must administer the account the App was installed on.",
  },
  unauthorized: {
    tone: "error",
    message:
      "GitHub could not confirm who installed the App, or the installer is not the GitHub account you are signed in with. Sign in with GitHub again and retry.",
  },
  conflict: {
    tone: "error",
    message: "This installation is already linked to another organization, so it was not linked here.",
  },
  unavailable: {
    tone: "error",
    message: "The GitHub App install flow is not configured on the server. Ask your administrator to set it up.",
  },
  error: {
    tone: "error",
    message: "Linking the installation failed on the server. Try again later.",
  },
};

/**
 * The outcome of a GitHub App setup redirect, read from a query string, or
 * null when the URL is not a setup redirect. Values are only ever matched
 * against known outcomes, never rendered, so the URL cannot inject text.
 */
export function readSetupResult(search: string): SetupResult | null {
  const params = new URLSearchParams(search);
  const outcome = params.get(SETUP_PARAM);
  if (outcome === null) return null;

  if (outcome === "linked") {
    const id = params.get(INSTALLATION_PARAM) ?? "";
    const suffix = /^[0-9]{1,19}$/.test(id) ? ` (installation ${id})` : "";
    return {
      outcome,
      tone: "success",
      message: `GitHub App installed and linked to your organization${suffix}. Repositories appear once GitHub has synced them.`,
    };
  }
  const known = OUTCOME_MESSAGES[outcome];
  if (known) return { outcome, ...known };
  return {
    outcome: "unknown",
    tone: "error",
    message: "GitHub App setup returned an unexpected result. Start the install again.",
  };
}

/**
 * `href` without the setup parameters (other parameters such as ?run= are
 * kept), as a path + query + hash suitable for history.replaceState. The
 * parameters are single-use: leaving them would repeat the message on
 * reload and leak the outcome into bookmarks and shared links.
 */
export function stripSetupParams(href: string): string {
  const url = new URL(href);
  url.searchParams.delete(SETUP_PARAM);
  url.searchParams.delete(INSTALLATION_PARAM);
  return `${url.pathname}${url.search}${url.hash}`;
}

/** An install URL request the user can act on; `message` is shown as is. */
export class InstallUrlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InstallUrlError";
  }
}

const SIGN_IN_AGAIN = "Your session has expired. Sign in again, then retry.";

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = await res.json();
    return typeof body?.detail === "string" ? body.detail : null;
  } catch {
    return null;
  }
}

/**
 * Only GitHub's own install page is followed. The backend builds this URL
 * from server configuration, but a redirect target from a response is
 * still checked rather than trusted (an open redirect if the API were
 * compromised or misrouted).
 */
export function isGitHubInstallUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.host === "github.com" && url.pathname.startsWith("/apps/");
  } catch {
    return false;
  }
}

/**
 * Ask the backend for a GitHub App install URL.
 *
 * Only the Supabase session is sent. No GitHub token is read, sent or
 * stored: the backend binds the install to the user's GitHub identity and
 * verifies ownership with the installer's OAuth code when GitHub calls back,
 * so the action keeps working after a reload or a session refresh.
 */
export async function requestInstallUrl(): Promise<string> {
  let res: Response;
  try {
    res = await authedFetch("/github/app/install-url", { method: "POST" });
  } catch (err) {
    if (err instanceof UnauthenticatedError) throw new InstallUrlError(SIGN_IN_AGAIN);
    throw new InstallUrlError("Could not reach the server to start the install. Try again.");
  }

  if (!res.ok) {
    const detail = await readDetail(res);
    if (res.status === 403) {
      throw new InstallUrlError(detail ?? "Only organization owners and admins can install the GitHub App.");
    }
    if (res.status === 503) {
      throw new InstallUrlError(
        "The GitHub App install flow is not configured on the server. Ask your administrator to set it up."
      );
    }
    if (res.status === 409) {
      // No GitHub identity is linked to this account (e.g. an email sign-in).
      throw new InstallUrlError(
        detail ?? "Your account has no linked GitHub identity. Sign in with GitHub, then install the App."
      );
    }
    throw new InstallUrlError(
      detail ? `Could not start the install: ${detail}` : `Could not start the install (HTTP ${res.status}).`
    );
  }

  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // Handled below as a missing URL.
  }
  const installUrl = (data as { install_url?: unknown } | null)?.install_url;
  if (!isGitHubInstallUrl(installUrl)) {
    throw new InstallUrlError("The server did not return a valid GitHub install link.");
  }
  return installUrl;
}

export interface GitHubInstallation {
  id: number;
  account_login: string;
  suspended_at: string | null;
}

export interface GitHubRepository {
  id: number;
  full_name: string;
  github_installation_id: number;
}

export interface GitHubAppState {
  role: string | null;
  installations: GitHubInstallation[];
  repositories: GitHubRepository[];
}

/**
 * The caller's org role and the org's installations and repositories, read
 * from Supabase under RLS. Uses the same membership as the backend's
 * get_current_user (earliest accepted membership), and scopes the lists to
 * that org so a member of several orgs sees the one the API acts for.
 */
export async function fetchGitHubAppState(userId: string): Promise<GitHubAppState> {
  const supabase = getBrowserClient();
  const membership = await supabase
    .from("organization_members")
    .select("org_id, role")
    .eq("user_id", userId)
    .not("joined_at", "is", null)
    .order("joined_at", { ascending: true })
    .limit(1);
  if (membership.error) throw new Error(membership.error.message);
  const member = (membership.data?.[0] ?? null) as { org_id: string; role: string | null } | null;
  if (!member) return { role: null, installations: [], repositories: [] };

  const [installations, repositories] = await Promise.all([
    supabase
      .from("github_installations")
      .select("id, account_login, suspended_at")
      .eq("org_id", member.org_id)
      .order("account_login", { ascending: true }),
    supabase
      .from("repositories")
      .select("id, full_name, github_installation_id")
      .eq("org_id", member.org_id)
      .order("full_name", { ascending: true }),
  ]);
  if (installations.error) throw new Error(installations.error.message);
  if (repositories.error) throw new Error(repositories.error.message);
  return {
    role: member.role,
    installations: (installations.data ?? []) as GitHubInstallation[],
    repositories: (repositories.data ?? []) as GitHubRepository[],
  };
}
