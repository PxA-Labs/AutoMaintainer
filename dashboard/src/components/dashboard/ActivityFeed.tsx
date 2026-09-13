import { Activity } from "lucide-react";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface ActivityFeedProps {
  activity: any[];
}

export function ActivityFeed({ activity }: ActivityFeedProps) {
  return (
    <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4 flex flex-col h-full min-h-[220px]">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wider flex items-center gap-2">
          <Activity className="w-3.5 h-3.5 text-emerald-400" aria-hidden="true" />
          Recent Activity
        </h3>
        <span className="text-[11px] font-mono text-zinc-500 tabular-nums">
          {activity.length} events
        </span>
      </div>

      <div className="flex-1 space-y-1.5 overflow-y-auto custom-scrollbar pr-1 max-h-[160px]">
        {activity.length > 0 ? (
          activity.slice(0, 15).map((a: any, i: number) => (
            <div
              key={i}
              className="flex items-start gap-2.5 p-2 bg-zinc-900/80 border border-zinc-800/40 rounded-lg text-xs"
            >
              <span className="text-[10px] font-mono text-zinc-500 shrink-0 mt-0.5 tabular-nums">
                {a.time || "now"}
              </span>
              <span className="text-zinc-300 leading-relaxed break-words">
                {a.title}
              </span>
            </div>
          ))
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center py-6 text-zinc-500 text-xs">
            <p>No recent activity</p>
            <p className="text-[11px] text-zinc-600 mt-1">Actions taken by agents appear here</p>
          </div>
        )}
      </div>
    </div>
  );
}
