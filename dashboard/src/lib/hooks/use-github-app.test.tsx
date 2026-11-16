import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

const fetchGitHubAppState = vi.fn();
const requestInstallUrl = vi.fn();
vi.mock("@/lib/github-app", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/github-app")>();
  return {
    ...actual,
    fetchGitHubAppState: (...args: unknown[]) => fetchGitHubAppState(...args),
    requestInstallUrl: (...args: unknown[]) => requestInstallUrl(...args),
  };
});

import { InstallUrlError } from "@/lib/github-app";
import { useGitHubApp } from "@/lib/hooks/use-github-app";

const ADMIN_STATE = {
  role: "owner",
  installations: [{ id: 42, account_login: "acme", suspended_at: null }],
  repositories: [],
};

beforeEach(() => {
  fetchGitHubAppState.mockReset().mockResolvedValue(ADMIN_STATE);
  requestInstallUrl.mockReset();
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("useGitHubApp", () => {
  it("reads the setup redirect, clears its parameters and reloads the lists", async () => {
    window.history.replaceState(null, "", "/?run=r1&github_setup=linked&github_installation_id=42");

    const { result } = renderHook(() => useGitHubApp("user-1"));

    await waitFor(() => expect(result.current.setupResult?.outcome).toBe("linked"));
    expect(window.location.search).toBe("?run=r1");
    await waitFor(() => expect(result.current.state).toEqual(ADMIN_STATE));
    expect(fetchGitHubAppState).toHaveBeenCalledWith("user-1");
    expect(result.current.isAdmin).toBe(true);

    act(() => result.current.dismissSetupResult());
    expect(result.current.setupResult).toBeNull();
  });

  it("does not load anything when signed out", async () => {
    const { result } = renderHook(() => useGitHubApp(null));
    await act(async () => {});
    expect(fetchGitHubAppState).not.toHaveBeenCalled();
    expect(result.current.state).toBeNull();
    expect(result.current.isAdmin).toBe(false);
  });

  it("is not admin for a member", async () => {
    fetchGitHubAppState.mockResolvedValue({ ...ADMIN_STATE, role: "member" });
    const { result } = renderHook(() => useGitHubApp("user-1"));
    await waitFor(() => expect(result.current.state?.role).toBe("member"));
    expect(result.current.isAdmin).toBe(false);
  });

  it("reports a load error", async () => {
    fetchGitHubAppState.mockRejectedValue(new Error("permission denied"));
    const { result } = renderHook(() => useGitHubApp("user-1"));
    await waitFor(() => expect(result.current.loadError).toBe("permission denied"));
  });

  it("install needs no GitHub token and navigates to the returned URL", async () => {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    requestInstallUrl.mockResolvedValue("https://github.com/apps/x/installations/new?state=s");
    const { result } = renderHook(() => useGitHubApp("user-1"));

    await act(async () => {
      await result.current.install();
    });

    expect(requestInstallUrl).toHaveBeenCalledWith();
    expect(assign).toHaveBeenCalledWith("https://github.com/apps/x/installations/new?state=s");
    expect(result.current.installing).toBe(true);
  });

  it("install failure is shown and the action re-enabled", async () => {
    requestInstallUrl.mockRejectedValue(new InstallUrlError("Only organization owners and admins can install"));
    const { result } = renderHook(() => useGitHubApp("user-1"));

    await act(async () => {
      await result.current.install();
    });

    expect(result.current.installError).toBe("Only organization owners and admins can install");
    expect(result.current.installing).toBe(false);
  });
});
