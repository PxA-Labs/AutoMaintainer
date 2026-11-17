import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// A signed-in session for authedFetch, and a fixed backend URL.
vi.mock("@/lib/supabase", () => ({
  getBrowserClient: () => ({
    auth: { getSession: async () => ({ data: { session: { access_token: "tok" } } }) },
  }),
}));
vi.mock("@/lib/config", () => ({ getBackendUrl: () => "https://api.example.test" }));

import { MergePolicyControl } from "./MergePolicyControl";
import { findOrgRepository, isWithinCeiling, type RepositoryMergePolicy } from "@/lib/merge-policy";

const REPO = { id: 4242, full_name: "acme/app" };
const URL = "https://api.example.test/repositories/4242/merge-policy";

function policy(overrides: Partial<RepositoryMergePolicy> = {}): RepositoryMergePolicy {
  return {
    repository_id: 4242,
    full_name: "acme/app",
    setting: null,
    ceiling: "ci",
    effective: "ci",
    allowed: ["human", "ci"],
    can_edit: true,
    ...overrides,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

async function select() {
  return (await screen.findByLabelText("Merge policy")) as HTMLSelectElement;
}

describe("MergePolicyControl", () => {
  it("renders nothing without an org repository", () => {
    const { container } = render(<MergePolicyControl repository={null} />);
    expect(container.innerHTML).toBe("");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the setting, the ceiling and the effective policy", async () => {
    fetchMock.mockResolvedValueOnce(json(policy({ setting: "human", effective: "human" })));
    render(<MergePolicyControl repository={REPO} />);

    const field = await select();
    expect(field.value).toBe("human");
    expect(fetchMock.mock.calls[0][0]).toBe(URL);
    const help = document.getElementById(field.getAttribute("aria-describedby")!)!;
    expect(help.textContent).toContain("Deployment limit: CI-gated");
    expect(help.textContent).toContain("Effective: Human review");
  });

  it("disables options above the deployment ceiling", async () => {
    fetchMock.mockResolvedValueOnce(json(policy()));
    render(<MergePolicyControl repository={REPO} />);

    const options = Array.from((await select()).options);
    expect(options.map((o) => [o.value, o.disabled])).toEqual([
      ["", false],
      ["human", false],
      ["ci", false],
      ["auto", true],
    ]);
    expect(options[0].textContent).toBe("Inherit (CI-gated)");
    expect(options[3].textContent).toContain("above deployment limit");
  });

  it("saves a new setting with PUT and shows the new effective policy", async () => {
    fetchMock
      .mockResolvedValueOnce(json(policy()))
      .mockResolvedValueOnce(json(policy({ setting: "human", effective: "human" })));
    render(<MergePolicyControl repository={REPO} />);

    fireEvent.change(await select(), { target: { value: "human" } });

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Effective: Human review"));
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe(URL);
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ merge_policy: "human" });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
    expect((await select()).value).toBe("human");
  });

  it("sends null to inherit the ceiling", async () => {
    fetchMock
      .mockResolvedValueOnce(json(policy({ setting: "human", effective: "human" })))
      .mockResolvedValueOnce(json(policy()));
    render(<MergePolicyControl repository={REPO} />);

    fireEvent.change(await select(), { target: { value: "" } });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ merge_policy: null });
  });

  it("is read-only for members", async () => {
    fetchMock.mockResolvedValueOnce(json(policy({ can_edit: false })));
    render(<MergePolicyControl repository={REPO} />);

    expect((await select()).disabled).toBe(true);
    expect(screen.getByText(/Only organization owners and admins can change it/)).toBeTruthy();
  });

  it("reports a refused save and keeps the stored value", async () => {
    fetchMock
      .mockResolvedValueOnce(json(policy()))
      .mockResolvedValueOnce(json({ detail: "merge_policy 'auto' is looser than MERGE_POLICY (ci)" }, 422));
    render(<MergePolicyControl repository={REPO} />);

    fireEvent.change(await select(), { target: { value: "human" } });

    expect((await screen.findByRole("alert")).textContent).toContain("looser than MERGE_POLICY");
    expect((await select()).value).toBe("");
  });

  it("shows who last changed the setting and when", async () => {
    const changedBy = "aaaaaaaa-0000-4000-8000-000000000001";
    fetchMock.mockResolvedValueOnce(
      json(policy({ last_change: { changed_by: changedBy, changed_at: "2026-11-17T10:00:00+00:00" } }))
    );
    render(<MergePolicyControl repository={REPO} />);

    const line = (await screen.findByText(/Last changed by/)).closest("p")!;
    expect(line.textContent).toContain("user aaaaaaaa");
    expect(line.querySelector(`[title="${changedBy}"]`)).toBeTruthy();
    expect(line.querySelector("time")?.getAttribute("dateTime")).toBe("2026-11-17T10:00:00+00:00");
  });

  it("shows no change line when the setting was never changed", async () => {
    fetchMock.mockResolvedValueOnce(json(policy({ last_change: null })));
    render(<MergePolicyControl repository={REPO} />);

    await select();
    expect(screen.queryByText(/Last changed by/)).toBeNull();
  });

  it("reports a repository the API does not find", async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: "Repository not found" }, 404));
    render(<MergePolicyControl repository={REPO} />);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "This repository is not linked to your organization."
    );
  });
});

describe("merge policy helpers", () => {
  it("orders policies from strictest to loosest", () => {
    expect(isWithinCeiling("human", "ci")).toBe(true);
    expect(isWithinCeiling("ci", "ci")).toBe(true);
    expect(isWithinCeiling("auto", "ci")).toBe(false);
  });

  it("finds the selected repository among the org's, by slug or URL", () => {
    const repos = [REPO, { id: 1, full_name: "acme/other" }];
    expect(findOrgRepository(repos, "Acme/App")).toBe(REPO);
    expect(findOrgRepository(repos, "https://github.com/acme/app")).toBe(REPO);
    expect(findOrgRepository(repos, "someone/else")).toBeNull();
    expect(findOrgRepository(repos, "owner/repo")).toBeNull();
    expect(findOrgRepository(undefined, "acme/app")).toBeNull();
  });
});
