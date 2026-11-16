import { beforeEach, describe, expect, it, vi } from "vitest";

/* eslint-disable @typescript-eslint/no-explicit-any */

// Fake Supabase client: a signed-in session for authedFetch, and a
// PostgREST query builder that records each table query and resolves with
// the rows configured per table.
const tables: Record<string, { data: any; error: any }> = {};
const queries: { table: string; calls: [string, ...unknown[]][] }[] = [];

function builder(table: string) {
  const entry = { table, calls: [] as [string, ...unknown[]][] };
  queries.push(entry);
  const chain: any = {};
  for (const name of ["select", "eq", "not", "order", "limit"]) {
    chain[name] = (...args: unknown[]) => {
      entry.calls.push([name, ...args]);
      return chain;
    };
  }
  chain.then = (resolve: (v: unknown) => void, reject: (e: unknown) => void) =>
    Promise.resolve(tables[table] ?? { data: [], error: null }).then(resolve, reject);
  return chain;
}

let accessToken: string | null = "supabase-token";
const client = {
  auth: {
    getSession: async () => ({
      data: { session: accessToken ? { access_token: accessToken } : null },
    }),
  },
  from: (table: string) => builder(table),
};
vi.mock("@/lib/supabase", () => ({ getBrowserClient: () => client }));
vi.mock("@/lib/config", () => ({ getBackendUrl: () => "https://api.example.test" }));

import {
  fetchGitHubAppState,
  isGitHubInstallUrl,
  isOrgAdmin,
  readSetupResult,
  requestInstallUrl,
  stripSetupParams,
} from "@/lib/github-app";

const INSTALL_URL = "https://github.com/apps/auto-maintainer/installations/new?state=abc.def";

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  accessToken = "supabase-token";
  queries.length = 0;
  for (const key of Object.keys(tables)) delete tables[key];
});

describe("readSetupResult", () => {
  it("ignores URLs that are not a setup redirect", () => {
    expect(readSetupResult("")).toBeNull();
    expect(readSetupResult("?run=abc")).toBeNull();
  });

  it("reports a linked installation as success with its id", () => {
    const result = readSetupResult("?github_setup=linked&github_installation_id=42");
    expect(result).toMatchObject({ outcome: "linked", tone: "success" });
    expect(result?.message).toContain("installation 42");
  });

  it("never renders a non-numeric installation id", () => {
    const result = readSetupResult("?github_setup=linked&github_installation_id=<b>x</b>");
    expect(result?.tone).toBe("success");
    expect(result?.message).not.toContain("<b>");
    expect(result?.message).not.toContain("installation");
  });

  it.each(["unverified", "invalid", "expired", "forbidden", "unauthorized", "conflict", "unavailable", "error"])(
    "reports %s as an error with its own reason",
    (outcome) => {
      const result = readSetupResult(`?github_setup=${outcome}`);
      expect(result).toMatchObject({ outcome, tone: "error" });
      expect(result?.message.length).toBeGreaterThan(20);
    }
  );

  it("reports a request for owner approval as information", () => {
    expect(readSetupResult("?github_setup=requested")).toMatchObject({ tone: "info" });
  });

  // Exact Location headers GET /github/app/setup sends: Python's
  // f"{_dashboard_base_url()}/?{urlencode(params)}" with the dicts returned by
  // main._complete_app_setup (params in insertion order).
  it.each([
    ["https://dash.example.test/?github_setup=linked&github_installation_id=12345678", "linked", "success"],
    ["https://dash.example.test/?github_setup=requested", "requested", "info"],
    ...["invalid", "unverified", "unavailable", "expired", "forbidden", "unauthorized", "conflict", "error"].map(
      (outcome) => [`https://dash.example.test/?github_setup=${outcome}`, outcome, "error"]
    ),
  ])("understands the setup route's redirect %s", (location, outcome, tone) => {
    const result = readSetupResult(new URL(location).search);
    expect(result).toMatchObject({ outcome, tone });
    if (outcome === "linked") expect(result?.message).toContain("installation 12345678");
    expect(stripSetupParams(location)).toBe("/");
  });

  it("does not echo an unknown outcome", () => {
    const result = readSetupResult("?github_setup=%3Cscript%3E");
    expect(result).toMatchObject({ outcome: "unknown", tone: "error" });
    expect(result?.message).not.toContain("script");
  });
});

describe("stripSetupParams", () => {
  it("removes only the setup parameters", () => {
    expect(
      stripSetupParams("https://app.test/?github_setup=linked&run=r1&github_installation_id=42#logs")
    ).toBe("/?run=r1#logs");
    expect(stripSetupParams("https://app.test/?github_setup=error")).toBe("/");
  });
});

describe("isOrgAdmin / isGitHubInstallUrl", () => {
  it("allows only owners and admins", () => {
    expect(isOrgAdmin("owner")).toBe(true);
    expect(isOrgAdmin("admin")).toBe(true);
    expect(isOrgAdmin("member")).toBe(false);
    expect(isOrgAdmin(null)).toBe(false);
  });

  it("accepts only https GitHub App pages", () => {
    expect(isGitHubInstallUrl(INSTALL_URL)).toBe(true);
    expect(isGitHubInstallUrl("http://github.com/apps/x/installations/new")).toBe(false);
    expect(isGitHubInstallUrl("https://github.com.evil.test/apps/x")).toBe(false);
    expect(isGitHubInstallUrl("https://evil.test/apps/x")).toBe(false);
    expect(isGitHubInstallUrl("javascript:alert(1)")).toBe(false);
    expect(isGitHubInstallUrl(undefined)).toBe(false);
  });
});

describe("requestInstallUrl", () => {
  it("POSTs with only the Supabase bearer token and returns the install URL", async () => {
    const fetchMock = vi.fn(async () => respond(200, { install_url: INSTALL_URL, expires_in: 600 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestInstallUrl()).resolves.toBe(INSTALL_URL);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.example.test/github/app/install-url");
    expect(init.method).toBe("POST");
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer supabase-token");
    // #481: no GitHub token is sent (Supabase drops provider_token on refresh).
    expect(headers.get("X-GitHub-User-Token")).toBeNull();
    expect([...headers.keys()].filter((name) => name.includes("github"))).toEqual([]);
  });

  it("asks to sign in again on a 401 (expired Supabase session)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(401, { detail: "Invalid or expired access token" })));
    await expect(requestInstallUrl()).rejects.toThrow(/session has expired. Sign in again/);
  });

  it("explains a missing GitHub identity (409)", async () => {
    const detail = "Your account has no linked GitHub identity. Sign in with GitHub, then install the App.";
    vi.stubGlobal("fetch", vi.fn(async () => respond(409, { detail })));
    await expect(requestInstallUrl()).rejects.toThrow(detail);

    vi.stubGlobal("fetch", vi.fn(async () => respond(409, {})));
    await expect(requestInstallUrl()).rejects.toThrow(/no linked GitHub identity/);
  });

  it("explains a 403 and a 503", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => respond(403, { detail: "Only organization owners and admins can install the GitHub App" }))
    );
    await expect(requestInstallUrl()).rejects.toThrow("Only organization owners and admins");

    vi.stubGlobal("fetch", vi.fn(async () => respond(503, { detail: "not configured" })));
    await expect(requestInstallUrl()).rejects.toThrow(/not configured on the server/);
  });

  it("reports other server errors with their status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(500, {})));
    await expect(requestInstallUrl()).rejects.toThrow("Could not start the install (HTTP 500).");
  });

  it("refuses to follow a URL that is not GitHub's install page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(200, { install_url: "https://evil.test/apps/x" })));
    await expect(requestInstallUrl()).rejects.toThrow(/valid GitHub install link/);
  });

  it("reports a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(requestInstallUrl()).rejects.toThrow(/Could not reach the server/);
  });
});

describe("fetchGitHubAppState", () => {
  it("reads the role, then the installations and repositories of that org", async () => {
    tables.organization_members = { data: [{ org_id: "org-1", role: "admin" }], error: null };
    tables.github_installations = {
      data: [{ id: 42, account_login: "acme", suspended_at: null }],
      error: null,
    };
    tables.repositories = {
      data: [{ id: 7, full_name: "acme/widgets", github_installation_id: 42 }],
      error: null,
    };

    const state = await fetchGitHubAppState("user-1");

    expect(state.role).toBe("admin");
    expect(state.installations).toHaveLength(1);
    expect(state.repositories[0].full_name).toBe("acme/widgets");
    const member = queries.find((q) => q.table === "organization_members")!;
    expect(member.calls).toContainEqual(["eq", "user_id", "user-1"]);
    // Pending invites (joined_at null) are not memberships.
    expect(member.calls).toContainEqual(["not", "joined_at", "is", null]);
    for (const table of ["github_installations", "repositories"]) {
      expect(queries.find((q) => q.table === table)!.calls).toContainEqual(["eq", "org_id", "org-1"]);
    }
  });

  it("returns empty lists without querying them when the user has no org", async () => {
    tables.organization_members = { data: [], error: null };
    await expect(fetchGitHubAppState("user-1")).resolves.toEqual({
      role: null,
      installations: [],
      repositories: [],
    });
    expect(queries.map((q) => q.table)).toEqual(["organization_members"]);
  });

  it("surfaces a query error", async () => {
    tables.organization_members = { data: [{ org_id: "org-1", role: "owner" }], error: null };
    tables.repositories = { data: null, error: { message: "permission denied" } };
    await expect(fetchGitHubAppState("user-1")).rejects.toThrow("permission denied");
  });
});
