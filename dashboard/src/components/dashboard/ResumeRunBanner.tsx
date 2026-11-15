"use client";

import { History, X } from "lucide-react";

interface ResumeRunBannerProps {
  runId: string;
  repoName: string;
  status: string;
  onResume: () => void;
  onDismiss: () => void;
}

/**
 * Offers to re-attach to a run that is still queued/running for the selected
 * repository but is not tracked by this tab (for example, it was started in
 * another tab, or the ?run= link was lost). Resuming is explicit rather than
 * automatic: the run may belong to a teammate in the same organization.
 */
export function ResumeRunBanner({
  runId,
  repoName,
  status,
  onResume,
  onDismiss,
}: ResumeRunBannerProps) {
  return (
    <div
      role="status"
      className="bg-indigo-500/10 border-b border-indigo-500/20 px-6 py-2.5 flex items-center gap-3 text-indigo-300 text-xs font-medium shrink-0"
    >
      <History className="w-4 h-4 shrink-0" aria-hidden="true" />
      <span className="flex-1">
        Run <span className="font-mono">{runId.substring(0, 8)}</span> for{" "}
        <span className="font-mono">{repoName}</span> is still {status}.
      </span>
      <button
        type="button"
        onClick={onResume}
        className="px-2.5 py-1 rounded-md bg-indigo-500/20 border border-indigo-500/30 text-indigo-200 hover:bg-indigo-500/30 transition-colors"
      >
        Resume tracking
      </button>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss resume suggestion"
        className="p-1 rounded-md text-indigo-300 hover:bg-indigo-500/20 transition-colors"
      >
        <X className="w-3.5 h-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
