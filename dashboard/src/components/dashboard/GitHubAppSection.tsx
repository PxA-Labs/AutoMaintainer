"use client";

import { Blocks, Loader2, RefreshCw } from "lucide-react";
import type { UseGitHubApp } from "@/lib/hooks/use-github-app";

type GitHubAppSectionProps = Pick<
  UseGitHubApp,
  "state" | "loading" | "loadError" | "isAdmin" | "installing" | "installError" | "install" | "refresh"
>;

/**
 * Sidebar section listing the org's GitHub App installations and the
 * number of repositories they give access to. Only org owners/admins get
 * the "Install GitHub App" action; the backend enforces the same rule.
 */
export function GitHubAppSection({
  state,
  loading,
  loadError,
  isAdmin,
  installing,
  installError,
  install,
  refresh,
}: GitHubAppSectionProps) {
  const installations = state?.installations ?? [];
  const repoCount = state?.repositories.length ?? 0;

  return (
    <section aria-labelledby="github-app-heading" className="space-y-2 pt-3 border-t border-zinc-800/40">
      <div className="flex items-center justify-between px-2">
        <h2 id="github-app-heading" className="text-[11px] font-semibold text-muted uppercase tracking-wider">
          GitHub App
        </h2>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          aria-label="Refresh GitHub App installations"
          className="p-1 rounded text-zinc-400 hover:text-zinc-200 disabled:opacity-60"
        >
          <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
        </button>
      </div>

      <div className="bg-zinc-900/60 border border-zinc-800/60 rounded-lg p-2 space-y-2 text-xs">
        {loadError ? (
          <p role="alert" className="text-red-400">
            Could not load GitHub App installations: {loadError}
          </p>
        ) : state === null ? (
          <p className="text-muted">{loading ? "Loading installations..." : "Not loaded."}</p>
        ) : installations.length === 0 ? (
          <p className="text-muted">No GitHub App installation is linked to your organization.</p>
        ) : (
          <>
            <ul aria-label="Linked GitHub App installations" className="space-y-1">
              {installations.map((inst) => (
                <li key={inst.id} className="flex items-center gap-1.5 text-zinc-300 font-mono truncate">
                  <Blocks className="w-3 h-3 shrink-0 text-zinc-400" aria-hidden="true" />
                  <span className="truncate">{inst.account_login}</span>
                  {inst.suspended_at ? <span className="text-amber-400 font-sans">(suspended)</span> : null}
                </li>
              ))}
            </ul>
            <p className="text-muted">
              {repoCount} {repoCount === 1 ? "repository" : "repositories"} available
            </p>
          </>
        )}

        {isAdmin && (
          <button
            type="button"
            onClick={() => void install()}
            disabled={installing}
            aria-busy={installing}
            className="w-full inline-flex items-center justify-center gap-1.5 px-2.5 py-1.5 rounded-md bg-zinc-100 text-zinc-900 font-medium hover:bg-white disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
          >
            {installing ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Blocks className="w-3.5 h-3.5" aria-hidden="true" />
            )}
            {installing ? "Opening GitHub..." : "Install GitHub App"}
          </button>
        )}
        {installError && (
          <p role="alert" className="text-red-400">
            {installError}
          </p>
        )}
      </div>
    </section>
  );
}
