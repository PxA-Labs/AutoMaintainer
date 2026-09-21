import { CheckCircle2, Zap, Clock } from "lucide-react";

interface SystemHealthProps {
  latency: number;
  tokensUsed: number;
}

export function SystemHealth({ latency, tokensUsed }: SystemHealthProps) {
  const latencyPercent = Math.min((latency / 5000) * 100, 100);
  const tokenPercent = Math.min((tokensUsed / 50000) * 100, 100);

  const getLatencyColor = (ms: number) => {
    if (ms === 0) return "bg-zinc-600";
    if (ms < 1000) return "bg-emerald-500";
    if (ms < 3000) return "bg-amber-500";
    return "bg-red-500";
  };

  return (
    <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4 flex flex-col h-full min-h-[220px]">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wider flex items-center gap-2">
          <CheckCircle2 className="w-3.5 h-3.5 text-indigo-400" aria-hidden="true" />
          System Health
        </h3>
        <span className="text-[11px] font-mono text-zinc-500">Live Telemetry</span>
      </div>

      <div className="flex-1 flex flex-col justify-center space-y-4">
        {/* Latency */}
        <div className="space-y-1.5">
          <div className="flex justify-between text-xs">
            <span className="text-zinc-400 flex items-center gap-1.5">
              <Clock className="w-3 h-3 text-zinc-500" aria-hidden="true" />
              Round-Trip Latency
            </span>
            <span className="font-mono text-zinc-200 tabular-nums">
              {latency > 0 ? `${latency}ms` : "idle"}
            </span>
          </div>
          <div className="h-1.5 bg-zinc-800/80 rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-500 rounded-full ${getLatencyColor(
                latency
              )}`}
              style={{ width: `${Math.max(latencyPercent, 4)}%` }}
            />
          </div>
        </div>

        {/* Tokens */}
        <div className="space-y-1.5">
          <div className="flex justify-between text-xs">
            <span className="text-zinc-400 flex items-center gap-1.5">
              <Zap className="w-3 h-3 text-zinc-500" aria-hidden="true" />
              Tokens Consumed
            </span>
            <span className="font-mono text-zinc-200 tabular-nums">
              {tokensUsed.toLocaleString()}
            </span>
          </div>
          <div className="h-1.5 bg-zinc-800/80 rounded-full overflow-hidden">
            <div
              className="h-full bg-indigo-500 transition-all duration-500 rounded-full"
              style={{ width: `${Math.max(tokenPercent, 2)}%` }}
            />
          </div>
        </div>

        {/* Model status */}
        <div className="pt-2 border-t border-zinc-800/40 flex items-center justify-between text-[11px] text-zinc-500 font-mono">
          <span>Engine</span>
          <span className="text-zinc-400">Llama-3-70b (Cloud)</span>
        </div>
      </div>
    </div>
  );
}
