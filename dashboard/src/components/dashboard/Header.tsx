import { Play, Square } from "lucide-react";
import { StatusDot } from "@/components/ui/StatusDot";

interface HeaderProps {
  isRunning: boolean;
  tokensUsed: number;
  latency: number;
  onStartStop: () => void;
}

export function Header({
  isRunning,
  tokensUsed,
  latency,
  onStartStop,
}: HeaderProps) {
  return (
    <header className="h-14 border-b border-zinc-800/60 flex items-center justify-between px-6 shrink-0 bg-background/80 backdrop-blur-sm">
      <div className="flex items-center gap-2.5 text-sm">
        <StatusDot status={isRunning ? "active" : "error"} pulse={isRunning} />
        <span className="text-zinc-400 text-xs">
          {isRunning ? "System active" : "System halted"}
        </span>
      </div>

      <div className="flex items-center gap-3">
        {tokensUsed > 0 && (
          <div className="flex items-center gap-3 text-xs font-mono text-zinc-500 bg-zinc-900/60 px-3 py-1.5 rounded-md border border-zinc-800/60">
            <span className="tabular-nums">
              {tokensUsed.toLocaleString()} tokens
            </span>
            <span className="text-zinc-700">|</span>
            <span className="tabular-nums">{latency}ms</span>
          </div>
        )}
        <button
          onClick={onStartStop}
          aria-label={isRunning ? "Stop agent loop" : "Start agent loop"}
          className={`inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-medium transition-colors active:scale-[0.98] ${
            isRunning
              ? "bg-red-500/15 text-red-400 hover:bg-red-500/25 border border-red-500/20"
              : "bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25 border border-emerald-500/20"
          }`}
        >
          {isRunning ? (
            <>
              <Square className="w-3.5 h-3.5" />
              Stop
            </>
          ) : (
            <>
              <Play className="w-3.5 h-3.5" />
              Start
            </>
          )}
        </button>
      </div>
    </header>
  );
}
