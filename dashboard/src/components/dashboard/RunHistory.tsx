"use client";

import { Activity, Loader2, GitCommit, AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/Badge";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface RunHistoryProps {
  runs: any[];
  loading: boolean;
  onRefresh: () => void;
  onSelectRun?: (runId: string) => void;
}

export function RunHistory({
  runs,
  loading,
  onRefresh,
  onSelectRun,
}: RunHistoryProps) {
  const getStatusVariant = (
    status: string
  ): "success" | "warning" | "error" | "info" | "default" => {
    switch (status) {
      case "completed":
        return "info";
      case "running":
        return "success";
      case "failed":
        return "error";
      case "cancelled":
        return "warning";
      default:
        return "default";
    }
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-6 max-w-6xl mx-auto w-full">
      <div className="flex items-center justify-between mb-6 shrink-0">
        <div>
          <h2 className="text-lg font-semibold text-zinc-100">
            Execution Run History
          </h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Audit log of autonomous orchestration pipelines executed on your repositories.
          </p>
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          aria-label="Refresh run history"
          className="flex items-center gap-2 px-3.5 py-1.5 bg-zinc-900 border border-zinc-800 text-zinc-300 rounded-lg text-xs font-medium hover:bg-zinc-800 transition-colors disabled:opacity-50 active:scale-[0.98]"
        >
          <Loader2
            className={`w-3.5 h-3.5 text-zinc-400 ${
              loading ? "animate-spin" : ""
            }`}
          />
          Refresh
        </button>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar">
        {loading && runs.length === 0 ? (
          <div className="flex items-center justify-center h-64">
            <Loader2 className="w-6 h-6 text-indigo-400 animate-spin" />
          </div>
        ) : runs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-zinc-500 border border-dashed border-zinc-800 rounded-xl p-8">
            <Activity className="w-8 h-8 mb-3 text-zinc-600" />
            <p className="text-sm font-medium text-zinc-400">No runs recorded yet</p>
            <p className="text-xs text-zinc-600 mt-1">
              Start an orchestration loop from the dashboard to populate historical runs.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {runs.map((run) => (
              <div
                key={run.id}
                onClick={() => onSelectRun?.(run.id)}
                className={`bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4 transition-all hover:border-zinc-700/80 ${
                  onSelectRun ? "cursor-pointer" : ""
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-xs font-medium text-zinc-300">
                      {run.id.substring(0, 8)}
                    </span>
                    <Badge variant={getStatusVariant(run.status)}>
                      {run.status}
                    </Badge>
                    <span className="text-xs text-zinc-400 font-mono">
                      {run.repo_name}
                    </span>
                  </div>
                  <span className="text-xs text-zinc-500 font-mono tabular-nums">
                    {new Date(run.created_at).toLocaleString()}
                  </span>
                </div>

                {run.error_message && (
                  <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 p-2.5 rounded-lg mt-2 flex items-start gap-2">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    <span>{run.error_message}</span>
                  </div>
                )}

                {run.result_summary && (
                  <div className="text-xs text-zinc-300 mt-2 bg-zinc-950/60 p-2.5 rounded-lg border border-zinc-800/40 font-mono leading-relaxed">
                    {run.result_summary}
                  </div>
                )}

                {run.pr_url && (
                  <div className="mt-2.5 flex items-center gap-2">
                    <a
                      href={run.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 font-medium"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <GitCommit className="w-3.5 h-3.5" />
                      View Generated Pull Request
                    </a>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
