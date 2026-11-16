"use client";

import {
  BrainCircuit,
  LayoutDashboard,
  GitGraph,
  Code2,
  Activity,
  GitBranch,
  Search,
  Shield,
} from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { UserMenu } from "@/components/ui/UserMenu";
import { StatusDot } from "@/components/ui/StatusDot";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface SidebarProps {
  user: any;
  onSignOut: () => void;
  activeTab: "dashboard" | "gitnexus" | "ide" | "runs";
  onSelectTab: (tab: "dashboard" | "gitnexus" | "ide" | "runs") => void;
  repoUrl: string;
  onRepoUrlChange: (url: string) => void;
  isEditingRepo: boolean;
  onSetIsEditingRepo: (editing: boolean) => void;
  targetIssue: string;
  onTargetIssueChange: (issue: string) => void;
  agentStatus: Record<string, string>;
  /** GitHub App status and install action (signed-in users only). */
  githubApp?: ReactNode;
}

const CREW = [
  { name: "Architect", role: "Principal Engineer" },
  { name: "Visionary", role: "Brainstormer" },
  { name: "Reviewer", role: "Product Manager" },
  { name: "Implementer", role: "Developer" },
  { name: "Maintainer", role: "Senior Engineer" },
];

/**
 * Repository input that keeps a local draft and only reports the value on
 * Enter or blur. Propagating every keystroke made the IDE reload its tree,
 * drop open tabs and the terminal open a new shell per character.
 */
function RepoInput({
  initialValue,
  onCommit,
  onCancel,
}: {
  initialValue: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initialValue);
  // Enter commits and unmounts the input; guard against a trailing blur
  // committing a second time.
  const doneRef = useRef(false);

  const finish = (commit: boolean) => {
    if (doneRef.current) return;
    doneRef.current = true;
    if (commit) onCommit(draft.trim());
    else onCancel();
  };

  return (
    <input
      id="target-repo-input"
      type="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(true);
        else if (e.key === "Escape") finish(false);
      }}
      className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-100 font-mono focus:outline-none focus:border-indigo-500"
      placeholder="e.g. facebook/react"
      // Mounted only after the user activates the edit button, which this
      // input replaces; focus must follow or it drops to <body>. Not a
      // page-load autofocus, which is what the rule guards against.
      // eslint-disable-next-line jsx-a11y/no-autofocus
      autoFocus
    />
  );
}

export function Sidebar({
  user,
  onSignOut,
  activeTab,
  onSelectTab,
  repoUrl,
  onRepoUrlChange,
  isEditingRepo,
  onSetIsEditingRepo,
  targetIssue,
  onTargetIssueChange,
  agentStatus,
  githubApp,
}: SidebarProps) {
  const navItems = [
    {
      id: "dashboard" as const,
      label: "Dashboard",
      icon: <LayoutDashboard className="w-4 h-4" />,
    },
    {
      id: "gitnexus" as const,
      label: "Code Graph",
      icon: <GitGraph className="w-4 h-4" />,
    },
    {
      id: "ide" as const,
      label: "Web IDE",
      icon: <Code2 className="w-4 h-4" />,
    },
    {
      id: "runs" as const,
      label: "Run History",
      icon: <Activity className="w-4 h-4" />,
    },
  ];

  return (
    <aside className="w-64 border-r border-zinc-800/60 bg-zinc-950/80 flex flex-col shrink-0 select-none">
      {/* Brand Header */}
      <div className="h-14 px-5 border-b border-zinc-800/60 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center">
            <BrainCircuit className="w-4 h-4 text-indigo-400" />
          </div>
          <span className="font-semibold text-sm text-zinc-100 tracking-tight">
            AutoMaintainer
          </span>
        </div>
        <span className="text-[10px] font-mono text-muted bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 rounded">
          v2.0
        </span>
      </div>

      {/* Navigation & Controls Area */}
      <div className="flex-1 py-4 px-3 space-y-6 overflow-y-auto custom-scrollbar">
        {/* Main Nav Tabs */}
        <nav className="space-y-0.5" aria-label="Main Navigation">
          {navItems.map((item) => {
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelectTab(item.id)}
                aria-current={isActive ? "page" : undefined}
                className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                  isActive
                    ? "bg-zinc-800 text-zinc-100 shadow-sm"
                    : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60"
                }`}
              >
                <span
                  className={isActive ? "text-indigo-400" : "text-zinc-500"}
                  aria-hidden="true"
                >
                  {item.icon}
                </span>
                {item.label}
              </button>
            );
          })}
        </nav>

        {/* Configuration Section */}
        <div className="space-y-3 pt-3 border-t border-zinc-800/40">
          <h2 className="text-[11px] font-semibold text-muted uppercase tracking-wider px-2">
            Target Repository
          </h2>

          <div className="space-y-2">
            {/* Target Repo Input */}
            <div className="bg-zinc-900/60 border border-zinc-800/60 rounded-lg p-2">
              {/* While not editing there is no input: the label then points at
                  the edit button, so clicking it still starts editing. */}
              <label
                htmlFor={isEditingRepo ? "target-repo-input" : "target-repo-edit"}
                className="text-[10px] font-medium text-muted flex items-center gap-1.5 mb-1 cursor-pointer"
              >
                <GitBranch className="w-3 h-3 text-zinc-400" />
                Repository Identifier
              </label>
              {isEditingRepo ? (
                <RepoInput
                  initialValue={repoUrl === "owner/repo" ? "" : repoUrl}
                  onCommit={(value) => {
                    if (value && value !== repoUrl) onRepoUrlChange(value);
                    onSetIsEditingRepo(false);
                  }}
                  onCancel={() => onSetIsEditingRepo(false)}
                />
              ) : (
                <button
                  id="target-repo-edit"
                  type="button"
                  onClick={() => onSetIsEditingRepo(true)}
                  aria-label={`Repository Identifier ${repoUrl || "owner/repo"} (edit)`}
                  className="block w-full text-left text-xs font-mono text-zinc-300 truncate cursor-pointer hover:text-white transition-colors py-0.5"
                  title="Click to edit target repository"
                >
                  {repoUrl || "owner/repo"}
                </button>
              )}
            </div>

            {/* Target Issue Input */}
            <div className="bg-zinc-900/60 border border-zinc-800/60 rounded-lg p-2">
              <label
                htmlFor="target-issue-input"
                className="text-[10px] font-medium text-muted flex items-center gap-1.5 mb-1"
              >
                <Search className="w-3 h-3 text-zinc-400" />
                Issue Number (Optional)
              </label>
              <input
                id="target-issue-input"
                type="text"
                value={targetIssue}
                onChange={(e) => onTargetIssueChange(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800/80 rounded px-2 py-1 text-xs text-zinc-100 font-mono placeholder:text-muted focus:outline-none focus:border-indigo-500"
                placeholder="#123 or blank"
              />
            </div>
          </div>
        </div>

        {githubApp}

        {/* Crew Roster Status */}
        <div className="space-y-2 pt-3 border-t border-zinc-800/40">
          <h2 className="text-[11px] font-semibold text-muted uppercase tracking-wider px-2">
            Engineering Crew
          </h2>
          <div className="space-y-1">
            {CREW.map((agent) => {
              const status = agentStatus[agent.name] || "idle";
              return (
                <div
                  key={agent.name}
                  className="flex items-center justify-between px-2.5 py-1.5 rounded-lg hover:bg-zinc-900/40 transition-colors text-xs"
                >
                  <div className="min-w-0 pr-2">
                    <p className="font-medium text-zinc-300 truncate">
                      {agent.name}
                    </p>
                    <p className="text-[10px] text-muted truncate">
                      {agent.role}
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <StatusDot status={status} size="sm" />
                    <span className="text-[10px] text-muted capitalize">
                      {status}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Admin Link */}
        <div className="pt-3 border-t border-zinc-800/40">
          <Link
            href="/admin"
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60 transition-colors"
          >
            <Shield className="w-4 h-4 text-zinc-500" />
            Admin Portal
          </Link>
        </div>
      </div>

      {/* Sidebar Footer User Info */}
      <div className="p-3 border-t border-zinc-800/60 shrink-0">
        <UserMenu user={user} onSignOut={onSignOut} />
      </div>
    </aside>
  );
}
