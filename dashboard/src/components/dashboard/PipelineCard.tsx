import { GitPullRequest } from "lucide-react";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface PipelineCardProps {
  pipeline: any[];
}

const STAGE_STYLES: Record<string, { bg: string; text: string }> = {
  architecting: { bg: "bg-rose-500/15", text: "text-rose-400" },
  ideating: { bg: "bg-emerald-500/15", text: "text-emerald-400" },
  reviewing: { bg: "bg-amber-500/15", text: "text-amber-400" },
  implementing: { bg: "bg-blue-500/15", text: "text-blue-400" },
  maintaining: { bg: "bg-purple-500/15", text: "text-purple-400" },
};

export function PipelineCard({ pipeline }: PipelineCardProps) {
  return (
    <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4 flex flex-col h-full min-h-[220px]">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wider flex items-center gap-2">
          <GitPullRequest className="w-3.5 h-3.5 text-indigo-400" aria-hidden="true" />
          Active Pipeline
        </h3>
        <span className="text-[11px] font-mono text-zinc-500 tabular-nums">
          {pipeline.length} items
        </span>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto custom-scrollbar pr-1 max-h-[160px]">
        {pipeline.length > 0 ? (
          pipeline.map((p: any) => {
            const stageStyle = STAGE_STYLES[p.status] || {
              bg: "bg-zinc-800",
              text: "text-zinc-400",
            };

            return (
              <div
                key={p.id}
                className="flex items-center gap-2.5 p-2 bg-zinc-900/80 border border-zinc-800/40 rounded-lg text-xs"
              >
                <span className="font-mono text-zinc-500 text-[11px] shrink-0">
                  #{p.id}
                </span>
                <span className="text-zinc-200 truncate flex-1 font-medium">
                  {p.title}
                </span>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider shrink-0 ${stageStyle.bg} ${stageStyle.text}`}
                >
                  {p.status}
                </span>
              </div>
            );
          })
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center py-6 text-zinc-500 text-xs">
            <p>No active pipeline tasks</p>
            <p className="text-[11px] text-zinc-600 mt-1">Start a run to trigger orchestration</p>
          </div>
        )}
      </div>
    </div>
  );
}
