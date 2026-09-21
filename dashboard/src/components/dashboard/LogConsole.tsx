"use client";

import { useEffect, useRef, useState } from "react";
import { Terminal, ArrowDownCircle } from "lucide-react";
import type { LogEntry } from "@/lib/hooks/use-agent-run";

interface LogConsoleProps {
  logs: LogEntry[];
  title?: string;
}

export function LogConsole({
  logs,
  title = "Agent Execution Log",
}: LogConsoleProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Auto scroll to bottom when new logs arrive if enabled
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 40;
    setAutoScroll(isAtBottom);
  };

  return (
    <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl overflow-hidden flex flex-col flex-1 min-h-[300px]">
      {/* Console Header */}
      <div className="px-4 py-2.5 bg-zinc-900/80 border-b border-zinc-800/60 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <Terminal className="w-3.5 h-3.5 text-zinc-400" aria-hidden="true" />
          <h3 className="text-xs font-semibold text-zinc-300 font-mono">
            {title}
          </h3>
        </div>
        <div className="flex items-center gap-3">
          {!autoScroll && (
            <button
              onClick={() => {
                setAutoScroll(true);
                if (scrollRef.current) {
                  scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
                }
              }}
              className="text-[11px] text-indigo-400 hover:text-indigo-300 flex items-center gap-1 font-mono transition-colors"
            >
              <ArrowDownCircle className="w-3 h-3" />
              Scroll to bottom
            </button>
          )}
          <span className="text-[11px] font-mono text-zinc-500 tabular-nums">
            {logs.length} {logs.length === 1 ? "entry" : "entries"}
          </span>
        </div>
      </div>

      {/* Log Output Body */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 p-4 font-mono text-xs overflow-y-auto custom-scrollbar bg-zinc-950/60 space-y-1 select-text"
      >
        {logs.map((log, i) => (
          <div
            key={i}
            className="flex items-start gap-2.5 py-0.5 leading-relaxed hover:bg-zinc-900/30 px-1 rounded transition-colors"
          >
            <span className="text-zinc-600 shrink-0 text-[11px] tabular-nums select-none">
              {log.time}
            </span>
            <span className="text-zinc-400 font-medium shrink-0 select-none">
              [{log.agent}]
            </span>
            <span className={`break-words flex-1 ${log.color || "text-zinc-300"}`}>
              {log.msg}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
