"use client";

import { useState } from "react";
import { Terminal, Code } from "lucide-react";
import WebIDE from "@/components/WebIDE";
import dynamic from "next/dynamic";
import type { LogEntry } from "@/lib/hooks/use-agent-run";

/* eslint-disable @typescript-eslint/no-explicit-any */

const InteractiveTerminal = dynamic(
  () => import("@/components/InteractiveTerminal"),
  { ssr: false }
);

interface IdeViewProps {
  user: any;
  repoUrl: string;
  logs: LogEntry[];
}

export function IdeView({ user, repoUrl, logs }: IdeViewProps) {
  const [terminalMode, setTerminalMode] = useState<"logs" | "pty">("logs");

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Monaco WebIDE container */}
      <div className="flex-1 min-h-0">
        {user ? (
          <WebIDE repoUrl={repoUrl} />
        ) : (
          <div className="flex items-center justify-center h-full bg-zinc-950 text-zinc-500 text-sm">
            Sign in to access the Web IDE
          </div>
        )}
      </div>

      {/* Bottom split console panel */}
      <div className="h-56 border-t border-zinc-800 bg-zinc-950 flex flex-col shrink-0">
        <div className="h-8 bg-zinc-900 border-b border-zinc-800 flex items-center px-4 gap-4 shrink-0">
          <button
            onClick={() => setTerminalMode("logs")}
            className={`flex items-center gap-1.5 text-xs font-mono transition-colors ${
              terminalMode === "logs"
                ? "text-zinc-200 font-medium"
                : "text-zinc-500 hover:text-zinc-400"
            }`}
          >
            <Code className="w-3.5 h-3.5" />
            agent_execution_log.sh
          </button>
          <div className="w-px h-3 bg-zinc-700"></div>
          <button
            onClick={() => setTerminalMode("pty")}
            className={`flex items-center gap-1.5 text-xs font-mono transition-colors ${
              terminalMode === "pty"
                ? "text-indigo-400 font-medium"
                : "text-zinc-500 hover:text-zinc-400"
            }`}
          >
            <Terminal className="w-3.5 h-3.5" />
            interactive_shell.pty
          </button>
        </div>

        {terminalMode === "logs" ? (
          <div className="flex-1 min-h-0 p-3 overflow-y-auto custom-scrollbar font-mono text-xs bg-zinc-950 space-y-1">
            {logs.map((log, i) => (
              <div key={i} className={`flex items-start gap-2 py-0.5 leading-relaxed ${log.color}`}>
                <span className="text-zinc-600 shrink-0 text-[11px] select-none">{log.time}</span>
                <span className="text-zinc-400 shrink-0 select-none">[{log.agent}]</span>
                <span className="break-words flex-1">{log.msg}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="flex-1 min-h-0">
            {user ? (
              <InteractiveTerminal repoUrl={repoUrl} />
            ) : (
              <div className="flex items-center justify-center h-full text-zinc-500 text-xs">
                Sign in to access the Interactive Terminal
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
