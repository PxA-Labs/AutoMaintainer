import { GitGraph, Network, Sparkles, Layers } from "lucide-react";

interface CodeGraphProps {
  repoUrl: string;
}

export function CodeGraph({ repoUrl }: CodeGraphProps) {
  return (
    <div className="flex-1 flex flex-col overflow-hidden p-6 max-w-6xl mx-auto w-full">
      <div className="mb-6 shrink-0">
        <h2 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
          <GitGraph className="w-5 h-5 text-indigo-400" />
          Repository Code Graph
          <span className="ml-1 px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider bg-amber-500/10 text-amber-400 border border-amber-500/20">
            Preview
          </span>
        </h2>
        <p className="text-xs text-muted mt-0.5">
          Planned knowledge graph view for{" "}
          <span className="font-mono text-zinc-400">{repoUrl || "the configured repository"}</span>.
          This screen is illustrative: no graph data is loaded yet.
        </p>
      </div>

      <div className="flex-1 bg-zinc-900/30 border border-zinc-800/60 rounded-xl p-8 flex flex-col items-center justify-center text-center">
        <div className="w-16 h-16 rounded-2xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center mb-4">
          <Network className="w-8 h-8 text-indigo-400" />
        </div>
        <h3 className="text-base font-medium text-zinc-200 mb-2">
          GitNexus Code Graph Engine
        </h3>
        <p className="text-xs text-muted max-w-md mb-6 leading-relaxed">
          The autonomous agents build an in-memory knowledge graph of symbols, imports, and AST paths to navigate large codebases with zero token bloat.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 max-w-xl w-full text-left">
          <div className="bg-zinc-900/80 border border-zinc-800/50 rounded-lg p-3">
            <div className="flex items-center gap-2 text-xs font-medium text-zinc-300 mb-1">
              <Layers className="w-3.5 h-3.5 text-indigo-400" />
              AST Indexing
            </div>
            <p className="text-[11px] text-muted">
              Symbols and references mapped on repository ingestion.
            </p>
          </div>
          <div className="bg-zinc-900/80 border border-zinc-800/50 rounded-lg p-3">
            <div className="flex items-center gap-2 text-xs font-medium text-zinc-300 mb-1">
              <Network className="w-3.5 h-3.5 text-emerald-400" />
              Call Hierarchy
            </div>
            <p className="text-[11px] text-muted">
              Deep function caller-callee dependency mapping.
            </p>
          </div>
          <div className="bg-zinc-900/80 border border-zinc-800/50 rounded-lg p-3">
            <div className="flex items-center gap-2 text-xs font-medium text-zinc-300 mb-1">
              <Sparkles className="w-3.5 h-3.5 text-amber-400" />
              Impact Analysis
            </div>
            <p className="text-[11px] text-muted">
              Downstream breakage prediction before writing code.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
