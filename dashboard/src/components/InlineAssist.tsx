/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import React, { useState, useEffect, useRef } from "react";
import { Sparkles, Check, X, Loader2, Play, Code2, Zap, FileCheck, HelpCircle } from "lucide-react";

export interface SelectionContext {
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
  selectedCode: string;
  prefixCode: string;
  suffixCode: string;
}

export interface InlineAssistProps {
  filePath: string;
  selectionContext: SelectionContext;
  position: { top: number; left: number };
  onClose: () => void;
  onAccept: (replacementCode: string) => void;
  onUpdatePreview?: (suggestion: string | null) => void;
  getBackendUrl: () => string;
  repoUrl: string;
}

const QUICK_ACTIONS = [
  { label: "Refactor for Performance", icon: Zap, prompt: "Refactor this code for better performance and readability." },
  { label: "Add Docstrings", icon: FileCheck, prompt: "Add clear docstrings and comments explaining this code." },
  { label: "Write Unit Tests", icon: Code2, prompt: "Generate unit tests for this code snippet." },
  { label: "Explain Code", icon: HelpCircle, prompt: "Explain what this code snippet does and suggest improvements." },
];

export default function InlineAssist({
  filePath,
  selectionContext,
  position,
  onClose,
  onAccept,
  onUpdatePreview,
  getBackendUrl,
  repoUrl,
}: InlineAssistProps) {
  const [prompt, setPrompt] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const [explanation, setExplanation] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onUpdatePreview?.(null);
      onClose();
    } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (suggestion !== null) {
        onAccept(suggestion);
      } else if (prompt.trim()) {
        handleSubmit(prompt);
      }
    } else if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      if (prompt.trim() && !isLoading) {
        handleSubmit(prompt);
      }
    }
  };

  const handleSubmit = async (customPrompt?: string) => {
    const finalPrompt = customPrompt || prompt;
    if (!finalPrompt.trim() || isLoading) return;

    setIsLoading(true);
    setError(null);
    setSuggestion("");
    onUpdatePreview?.("");

    try {
      const backendUrl = getBackendUrl();
      const res = await fetch(`${backendUrl}/assist/inline`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo_name: repoUrl,
          file_path: filePath,
          prompt: finalPrompt,
          selected_code: selectionContext.selectedCode,
          prefix_code: selectionContext.prefixCode,
          suffix_code: selectionContext.suffixCode,
          selection: {
            startLine: selectionContext.startLine,
            startColumn: selectionContext.startColumn,
            endLine: selectionContext.endLine,
            endColumn: selectionContext.endColumn,
          },
        }),
      });

      if (!res.ok) {
        throw new Error(`Inline assist request failed with status ${res.status}`);
      }

      if (!res.body) {
        throw new Error("No response stream body available");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let accumulated = "";
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const part of parts) {
          const trimmed = part.trim();
          if (trimmed.startsWith("data: ")) {
            const dataStr = trimmed.slice(6);
            if (dataStr === "[DONE]") break;
            try {
              const parsed = JSON.parse(dataStr);
              if (parsed.error) {
                throw new Error(parsed.error);
              }
              if (parsed.content) {
                accumulated += parsed.content;
                setSuggestion(accumulated);
                onUpdatePreview?.(accumulated);
              }
            } catch (e: any) {
              if (e.message && !e.message.includes("JSON")) {
                throw e;
              }
            }
          }
        }
      }

      let finalCode = accumulated;
      if (finalCode.startsWith("```")) {
        finalCode = finalCode.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
      }
      setSuggestion(finalCode);
      onUpdatePreview?.(finalCode);
    } catch (err: any) {
      setError(err.message || "Failed to generate inline edit");
      onUpdatePreview?.(null);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div
      className="absolute z-50 w-[520px] bg-[#1e1e2e] border border-indigo-500/40 rounded-xl shadow-2xl overflow-hidden font-sans text-zinc-100 backdrop-blur-md transition-all"
      style={{
        top: Math.max(10, Math.min(position.top, window.innerHeight - 380)),
        left: Math.max(20, Math.min(position.left, window.innerWidth - 560)),
      }}
      onKeyDown={handleKeyDown}
    >
      {/* Header Bar */}
      <div className="bg-[#181825] px-4 py-2.5 border-b border-zinc-800 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-medium text-indigo-400">
          <Sparkles className="w-4 h-4 text-indigo-400 animate-pulse" />
          <span>Inline Assist</span>
          <span className="text-zinc-500 font-mono text-[11px]">
            ({filePath}:{selectionContext.startLine}-{selectionContext.endLine})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-zinc-500 font-mono bg-zinc-800/80 px-1.5 py-0.5 rounded">
            Esc to cancel
          </span>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-white p-1 rounded-md hover:bg-zinc-800 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Main Body */}
      <div className="p-4 space-y-3">
        {/* Quick Action Pills */}
        {suggestion === null && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {QUICK_ACTIONS.map((action, idx) => {
              const Icon = action.icon;
              return (
                <button
                  key={idx}
                  onClick={() => {
                    setPrompt(action.prompt);
                    handleSubmit(action.prompt);
                  }}
                  disabled={isLoading}
                  className="flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-full bg-zinc-800/80 hover:bg-indigo-600/30 text-zinc-300 hover:text-indigo-200 border border-zinc-700/60 hover:border-indigo-500/50 transition-all disabled:opacity-50"
                >
                  <Icon className="w-3 h-3 text-indigo-400" />
                  <span>{action.label}</span>
                </button>
              );
            })}
          </div>
        )}

        {/* Input Field & Submit */}
        {suggestion === null ? (
          <div className="relative flex items-center">
            <input
              ref={inputRef}
              type="text"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Ask AI to edit or generate code... (Cmd+K)"
              disabled={isLoading}
              className="w-full bg-[#11111b] border border-zinc-700/80 focus:border-indigo-500 rounded-lg pl-3 pr-10 py-2 text-sm text-white placeholder-zinc-500 focus:outline-none transition-colors"
            />
            <button
              onClick={() => handleSubmit()}
              disabled={!prompt.trim() || isLoading}
              className="absolute right-2 p-1.5 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white transition-colors"
            >
              {isLoading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Play className="w-3.5 h-3.5 fill-current" />
              )}
            </button>
          </div>
        ) : null}

        {/* Error Notification */}
        {error && (
          <div className="text-xs text-red-400 bg-red-950/40 border border-red-900/60 rounded-lg p-2.5">
            {error}
          </div>
        )}

        {/* Generated Code Suggestion Preview */}
        {suggestion !== null && (
          <div className="space-y-3">
            {explanation && (
              <div className="text-xs text-zinc-300 bg-indigo-950/30 border border-indigo-900/40 rounded-lg p-2.5 leading-relaxed">
                <span className="font-semibold text-indigo-300 mr-1">Explanation:</span>
                {explanation}
              </div>
            )}
            <div className="space-y-1">
              <div className="flex items-center justify-between text-[11px] text-zinc-400 font-mono">
                <span>Proposed Change Preview:</span>
                <span className="text-emerald-400">Ready to Accept</span>
              </div>
              <div className="max-h-48 overflow-y-auto bg-[#11111b] border border-zinc-800 rounded-lg p-3 font-mono text-xs text-emerald-300 leading-relaxed custom-scrollbar">
                <pre className="whitespace-pre-wrap">{suggestion}</pre>
              </div>
            </div>

            {/* Action Bar: Accept / Reject */}
            <div className="flex items-center justify-between pt-1">
              <button
                onClick={() => setSuggestion(null)}
                className="text-xs text-zinc-400 hover:text-zinc-200 transition-colors"
              >
                ← Refine prompt
              </button>
              <div className="flex items-center gap-2">
                <button
                  onClick={onClose}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors"
                >
                  <X className="w-3.5 h-3.5" />
                  <span>Reject (Esc)</span>
                </button>
                <button
                  onClick={() => onAccept(suggestion)}
                  className="flex items-center gap-1 px-3.5 py-1.5 text-xs font-medium rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-lg shadow-emerald-600/20 transition-colors"
                >
                  <Check className="w-3.5 h-3.5" />
                  <span>Accept (⌘↵)</span>
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
