"use client";

import { useState, useCallback } from "react";
import { getBackendUrl, parseTargetIssue } from "@/lib/config";
import { useAuth } from "@/lib/auth";

interface AgentRunState {
  isRunning: boolean;
  activeRunId: string | null;
  repoUrl: string;
  targetIssue: string;
  isEditingRepo: boolean;
}

interface AgentRunActions {
  startStop: () => Promise<void>;
  setRepoUrl: (url: string) => void;
  setTargetIssue: (issue: string) => void;
  setIsEditingRepo: (editing: boolean) => void;
  setActiveRunId: (id: string | null) => void;
  resetRun: () => void;
  addLog: (log: LogEntry) => void;
}

export interface LogEntry {
  time: string;
  agent: string;
  msg: string;
  color: string;
}

interface UseAgentRunOptions {
  onRequireAuth?: () => void;
  onStart?: () => void;
}

export function useAgentRun(
  onLog: (entry: LogEntry) => void,
  options?: UseAgentRunOptions
) {
  const { user, session } = useAuth();
  const [isRunning, setIsRunning] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [repoUrl, setRepoUrl] = useState("owner/repo");
  const [targetIssue, setTargetIssue] = useState("");
  const [isEditingRepo, setIsEditingRepo] = useState(false);

  const log = useCallback(
    (msg: string, color: string, agent = "System") => {
      onLog({
        time: new Date().toLocaleTimeString(),
        agent,
        msg,
        color,
      });
    },
    [onLog]
  );

  const resetRun = useCallback(() => {
    setActiveRunId(null);
    setIsRunning(false);
  }, []);

  const startStop = useCallback(async () => {
    if (!user) {
      log("Please sign in with GitHub to start an agent run.", "text-amber-400");
      options?.onRequireAuth?.();
      return;
    }

    // The backend authorizes run controls with the Supabase access token, so a
    // signed-in user without an active session cannot start or stop a run.
    if (!session?.access_token) {
      log(
        "Your session has expired. Please sign in again before starting or stopping an agent run.",
        "text-red-400"
      );
      options?.onRequireAuth?.();
      return;
    }

    const authHeader = `Bearer ${session.access_token}`;

    if (!isRunning) {
      if (
        !repoUrl ||
        repoUrl.trim() === "owner/repo" ||
        repoUrl.trim() === ""
      ) {
        log(
          "Please configure a valid Target Repository in the sidebar first.",
          "text-red-400"
        );
        setIsEditingRepo(true);
        return;
      }

      let targetIssueNumber: number | null;
      try {
        targetIssueNumber = parseTargetIssue(targetIssue);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Invalid target issue.";
        log(message, "text-red-400");
        return;
      }

      options?.onStart?.();
      setIsRunning(true);
      log(`Triggering AI Agent Loop for ${repoUrl}...`, "text-zinc-500");

      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/start`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: authHeader,
          },
          body: JSON.stringify({
            repo_name: repoUrl,
            target_issue: targetIssueNumber,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          const detail =
            typeof data.detail === "string"
              ? data.detail
              : `Backend returned HTTP ${res.status}.`;
          throw new Error(detail);
        }
        if (!data.run_id) {
          throw new Error("Backend did not return a run identifier");
        }
        setActiveRunId(data.run_id);
        log(
          `Agent run queued: ${data.run_id.substring(0, 8)}...`,
          "text-emerald-500"
        );
      } catch (err) {
        console.error(err);
        log(
          `Failed to reach backend. Is it running? (${err})`,
          "text-red-400"
        );
        setIsRunning(false);
      }
    } else {
      setIsRunning(false);
      log("Agent Loop Halted.", "text-red-500");
      try {
        const backendUrl = getBackendUrl();
        const res = activeRunId
          ? await fetch(`${backendUrl}/stop`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: authHeader,
              },
              body: JSON.stringify({ run_id: activeRunId }),
            })
          : await fetch(`${backendUrl}/stop`, {
              method: "POST",
              headers: { Authorization: authHeader },
            });

        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : `Backend returned HTTP ${res.status}.`
          );
        }
      } catch (err) {
        console.error("Failed to stop agents:", err);
        log(`Failed to stop the agent run. (${err})`, "text-red-400");
      }
    }
  }, [isRunning, repoUrl, targetIssue, activeRunId, user, session, log, options]);

  const state: AgentRunState = {
    isRunning,
    activeRunId,
    repoUrl,
    targetIssue,
    isEditingRepo,
  };

  const actions: AgentRunActions = {
    startStop,
    setRepoUrl,
    setTargetIssue,
    setIsEditingRepo,
    setActiveRunId,
    resetRun,
    addLog: onLog,
  };

  return { state, actions };
}
