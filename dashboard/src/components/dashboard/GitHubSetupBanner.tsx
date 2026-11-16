"use client";

import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import type { SetupResult } from "@/lib/github-app";

const TONES = {
  success: {
    className: "bg-emerald-500/10 border-emerald-500/20 text-emerald-300",
    Icon: CheckCircle2,
  },
  info: {
    className: "bg-indigo-500/10 border-indigo-500/20 text-indigo-300",
    Icon: Info,
  },
  error: {
    className: "bg-red-500/10 border-red-500/20 text-red-300",
    Icon: AlertCircle,
  },
} as const;

/**
 * Reports the outcome of the GitHub App setup redirect. A failure is an
 * alert (announced immediately); success and "requested" are a polite
 * status message.
 */
export function GitHubSetupBanner({
  result,
  onDismiss,
}: {
  result: SetupResult;
  onDismiss: () => void;
}) {
  const { className, Icon } = TONES[result.tone];
  return (
    <div
      role={result.tone === "error" ? "alert" : "status"}
      data-outcome={result.outcome}
      className={`border-b px-6 py-2.5 flex items-center gap-3 text-xs font-medium shrink-0 ${className}`}
    >
      <Icon className="w-4 h-4 shrink-0" aria-hidden="true" />
      <span className="flex-1">
        {result.tone === "error" ? "GitHub App not linked. " : null}
        {result.message}
      </span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss GitHub App setup message"
        className="p-1 rounded-md hover:bg-white/10 transition-colors"
      >
        <X className="w-3.5 h-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
