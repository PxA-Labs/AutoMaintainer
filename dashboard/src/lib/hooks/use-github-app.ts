"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchGitHubAppState,
  InstallUrlError,
  isOrgAdmin,
  readSetupResult,
  requestInstallUrl,
  stripSetupParams,
  type GitHubAppState,
  type SetupResult,
} from "@/lib/github-app";

export interface UseGitHubApp {
  /** null until the first load finishes (or when signed out). */
  state: GitHubAppState | null;
  loading: boolean;
  loadError: string | null;
  isAdmin: boolean;
  /** Outcome of the GitHub App setup redirect this page was opened with. */
  setupResult: SetupResult | null;
  dismissSetupResult: () => void;
  installing: boolean;
  installError: string | null;
  /** Request an install URL and leave for GitHub. */
  install: () => Promise<void>;
  refresh: () => Promise<void>;
}

/**
 * State for the GitHub App install flow: the caller's role, the org's
 * installations and repositories, the setup redirect outcome and the
 * install action. `userId` is the signed-in Supabase user (null when
 * signed out). The install action needs only the Supabase session.
 */
export function useGitHubApp(userId: string | null): UseGitHubApp {
  // Results are tagged with the user they were loaded for, so a sign-out or
  // account switch never shows the previous user's lists.
  const [loaded, setLoaded] = useState<{ userId: string; state: GitHubAppState } | null>(null);
  const [failed, setFailed] = useState<{ userId: string; message: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [setupResult, setSetupResult] = useState<SetupResult | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);
  // Only the latest load may write state (a refresh can overlap the first load).
  const loadIdRef = useRef(0);

  // Read the setup redirect once, then remove its parameters from the URL.
  // Runs after mount because the static export has no URL at build time.
  useEffect(() => {
    const result = readSetupResult(window.location.search);
    if (!result) return;
    window.history.replaceState(null, "", stripSetupParams(window.location.href));
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time read of the URL on mount
    setSetupResult(result);
  }, []);

  // Resolves once the load has been applied (or superseded); never rejects.
  const load = useCallback((forUser: string): Promise<void> => {
    const loadId = ++loadIdRef.current;
    return fetchGitHubAppState(forUser).then(
      (next) => {
        if (loadId !== loadIdRef.current) return;
        setLoaded({ userId: forUser, state: next });
        setFailed(null);
      },
      (err: unknown) => {
        if (loadId !== loadIdRef.current) return;
        setFailed({ userId: forUser, message: err instanceof Error ? err.message : String(err) });
      }
    );
  }, []);

  // Load on sign-in. This also runs right after the setup redirect lands,
  // so the lists include an installation the redirect just linked.
  useEffect(() => {
    if (userId) void load(userId);
  }, [userId, load]);

  const refresh = useCallback(async () => {
    if (!userId) return;
    setRefreshing(true);
    try {
      await load(userId);
    } finally {
      setRefreshing(false);
    }
  }, [userId, load]);

  const install = useCallback(async () => {
    setInstalling(true);
    setInstallError(null);
    try {
      const url = await requestInstallUrl();
      window.location.assign(url);
      // Stay "installing" while the browser navigates away.
    } catch (err) {
      setInstallError(
        err instanceof InstallUrlError ? err.message : "Could not start the GitHub App install. Try again."
      );
      setInstalling(false);
    }
  }, []);

  const dismissSetupResult = useCallback(() => setSetupResult(null), []);

  const state = userId && loaded?.userId === userId ? loaded.state : null;
  const loadError = userId && failed?.userId === userId ? failed.message : null;

  return {
    state,
    loading: refreshing || (!!userId && state === null && loadError === null),
    loadError,
    isAdmin: isOrgAdmin(state?.role),
    setupResult,
    dismissSetupResult,
    installing,
    installError,
    install,
    refresh,
  };
}
