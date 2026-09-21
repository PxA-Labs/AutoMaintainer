import { PipelineCard } from "./PipelineCard";
import { ActivityFeed } from "./ActivityFeed";
import { SystemHealth } from "./SystemHealth";
import { AgentGrid } from "./AgentGrid";
import { LogConsole } from "./LogConsole";
import type { LogEntry } from "@/lib/hooks/use-agent-run";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface DashboardViewProps {
  pipeline: any[];
  activity: any[];
  systemHealth: { latency: number; tokensUsed: number };
  agentStatus: Record<string, string>;
  logs: LogEntry[];
}

export function DashboardView({
  pipeline,
  activity,
  systemHealth,
  agentStatus,
  logs,
}: DashboardViewProps) {
  return (
    <div className="flex-1 flex flex-col overflow-y-auto custom-scrollbar p-6 space-y-6">
      {/* 3-Column Metrics Overview */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 shrink-0">
        <PipelineCard pipeline={pipeline} />
        <ActivityFeed activity={activity} />
        <SystemHealth
          latency={systemHealth.latency}
          tokensUsed={systemHealth.tokensUsed}
        />
      </div>

      {/* 5-Agent Status Strip */}
      <div className="shrink-0">
        <div className="mb-2.5 flex items-center justify-between">
          <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
            Autonomous Engineering Crew
          </h2>
          <span className="text-[11px] text-zinc-500 font-mono">
            5 Active Specialists
          </span>
        </div>
        <AgentGrid agentStatus={agentStatus} />
      </div>

      {/* Execution Log Console */}
      <div className="flex-1 flex flex-col min-h-[320px]">
        <LogConsole logs={logs} />
      </div>
    </div>
  );
}
