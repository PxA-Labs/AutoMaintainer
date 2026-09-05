"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Sidebar } from "./Sidebar";
import { Header } from "./Header";
import { DashboardView } from "./DashboardView";
import { IdeView } from "./IdeView";
import { RunHistory } from "./RunHistory";
import { CodeGraph } from "./CodeGraph";
import { AuthGate } from "./AuthGate";
import { useAuth } from "@/lib/auth";
import { useAgentRun, type LogEntry } from "@/lib/hooks/use-agent-run";
import { useLogs } from "@/lib/hooks/use-logs";
import { useSupabaseHealth } from "@/lib/hooks/use-health";
import { getBackendUrl } from "@/lib/config";
import { AlertCircle, Loader2 } from "lucide-react";

/* eslint-disable @typescript-eslint/no-explicit-any */

export function DashboardShell() {
  const { user, session, loading: authLoading, signInWithGitHub, signOut } = useAuth();
  const [skipAuth, setSkipAuth] = useState(false);
  const [activeTab, setActiveTab] = useState<"dashboard" | "gitnexus" | "ide" | "runs">("dashboard");
  const [runs, setRuns] = useState<any[]>([]);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  // Health check for Supabase persistence
  const isSupabaseUnreachable = useSupabaseHealth();

  // Forward ref for logging
  const addLogRef = useRef<(entry: LogEntry) => void>(() => {});

  // Agent run controller hook
  const { state: runState, actions: runActions } = useAgentRun((entry) => {
    addLogRef.current(entry);
  });

  const effectiveRunId = selectedRunId || runState.activeRunId;

  // Realtime logs and telemetry hook
  const {
    logs,
    systemHealth,
    pipeline,
    activity,
    agentStatus,
    addLog,
    resetState: resetLogsState,
  } = useLogs(effectiveRunId);

  useEffect(() => {
    addLogRef.current = addLog;
  }, [addLog]);

  // Wipe logs and run state when signing out
  const handleSignOut = useCallback(() => {
    resetLogsState();
    runActions.resetRun();
    setSelectedRunId(null);
    setRuns([]);
    signOut();
  }, [resetLogsState, runActions, signOut]);

  // Fetch runs list from backend
  const fetchRuns = useCallback(async () => {
    if (!session) return;
    setLoadingRuns(true);
    try {
      const backendUrl = getBackendUrl();
      const res = await fetch(`${backendUrl}/runs`, {
        headers: { Authorization: `Bearer ${session.access_token}` },
      });
      const data = await res.json();
      setRuns(data.runs || []);
    } catch (err) {
      console.error("Failed to fetch runs:", err);
    } finally {
      setLoadingRuns(false);
    }
  }, [session]);

  // Initial runs fetch on authenticated session
  useEffect(() => {
    let isSubscribed = true;
    if (session) {
      const load = async () => {
        try {
          const backendUrl = getBackendUrl();
          const res = await fetch(`${backendUrl}/runs`, {
            headers: { Authorization: `Bearer ${session.access_token}` },
          });
          const data = await res.json();
          if (isSubscribed) {
            setRuns(data.runs || []);
          }
        } catch (err) {
          console.error("Failed to fetch runs:", err);
        }
      };
      load();
    }
    return () => {
      isSubscribed = false;
    };
  }, [session]);

  // Loading state during auth check
  if (authLoading) {
    return (
      <div className="flex min-h-dvh w-full bg-zinc-950 items-center justify-center">
        <Loader2 className="w-7 h-7 text-indigo-400 animate-spin" />
      </div>
    );
  }

  // Auth gate for non-authenticated visitors
  if (!user && !skipAuth) {
    return (
      <AuthGate
        onSignIn={signInWithGitHub}
        onSkip={() => setSkipAuth(true)}
        loading={authLoading}
      />
    );
  }

  return (
    <div className="flex h-screen w-full bg-zinc-950 text-zinc-100 font-sans overflow-hidden antialiased">
      {/* Sidebar Navigation */}
      <Sidebar
        user={user}
        onSignOut={handleSignOut}
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        repoUrl={runState.repoUrl}
        onRepoUrlChange={runActions.setRepoUrl}
        isEditingRepo={runState.isEditingRepo}
        onSetIsEditingRepo={runActions.setIsEditingRepo}
        targetIssue={runState.targetIssue}
        onTargetIssueChange={runActions.setTargetIssue}
        agentStatus={agentStatus}
      />

      {/* Main Content Viewport */}
      <div className="flex-1 flex flex-col h-screen overflow-hidden relative bg-zinc-950">
        {/* Supabase Outage Banner */}
        {isSupabaseUnreachable && (
          <div
            role="alert"
            className="bg-amber-500/10 border-b border-amber-500/20 px-6 py-2.5 flex items-center gap-2.5 text-amber-400 text-xs font-medium shrink-0"
          >
            <AlertCircle className="w-4 h-4 shrink-0" />
            <span>
              Supabase database is currently unreachable or paused. Historical logs and persistence are disabled.
            </span>
          </div>
        )}

        {/* Global Header */}
        <Header
          isRunning={runState.isRunning}
          tokensUsed={systemHealth.tokensUsed}
          latency={systemHealth.latency}
          onStartStop={runActions.startStop}
        />

        {/* Main View Switcher */}
        <main className="flex-1 flex flex-col overflow-hidden">
          {activeTab === "ide" ? (
            <IdeView user={user} repoUrl={runState.repoUrl} logs={logs} />
          ) : activeTab === "gitnexus" ? (
            <CodeGraph repoUrl={runState.repoUrl} />
          ) : activeTab === "runs" ? (
            <RunHistory
              runs={runs}
              loading={loadingRuns}
              onRefresh={fetchRuns}
              onSelectRun={(runId) => {
                setSelectedRunId(runId);
                setActiveTab("dashboard");
              }}
            />
          ) : (
            <DashboardView
              pipeline={pipeline}
              activity={activity}
              systemHealth={systemHealth}
              agentStatus={agentStatus}
              logs={logs}
            />
          )}
        </main>
      </div>
    </div>
  );
}
