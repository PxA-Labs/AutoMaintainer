"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { getBackendUrl, parseTargetIssue } from "@/lib/config";
import { useAuth } from "@/lib/auth";
import { isTerminalRunStatus } from "@/lib/run-status";

/** How often the active run's status is polled from GET /status/{run_id}. */
export const RUN_STATUS_POLL_MS = 5000;

/**
 * idle      - no run is tracked
 * starting  - POST /start is in flight (no run_id yet, so Stop is impossible)
 * running   - the backend confirmed a run_id and it is queued/running
 * stopping  - POST /stop is in flight; we stay "running" until it succeeds
 */
export type RunPhase = "idle" | "starting" | "running" | "stopping";

interface AgentRunState {
  phase: RunPhase;
  isRunning: boolean;
  /** True while a start or stop request is in flight; the control is disabled. */
  isPending: boolean;
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

async function readDetail(res: Response): Promise<string> {
  const data = await res.json().catch(() => ({}));
  return typeof data?.detail === "string"
    ? data.detail
    : `Backend returned HTTP ${res.status}.`;
}

export function useAgentRun(
  onLog: (entry: LogEntry) => void,
  options?: UseAgentRunOptions
) {
  const { user, session } = useAuth();
  const [phase, setPhase] = useState<RunPhase>("idle");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [repoUrl, setRepoUrl] = useState("owner/repo");
  const [targetIssue, setTargetIssue] = useState("");
  const [isEditingRepo, setIsEditingRepo] = useState(false);

  // The phase is mirrored in a ref so that two clicks delivered in the same
  // frame (before React re-renders) cannot both see "idle" and start two runs.
  const phaseRef = useRef<RunPhase>("idle");
  const updatePhase = useCallback((next: RunPhase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

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
    updatePhase("idle");
  }, [updatePhase]);

  const accessToken = session?.access_token ?? null;

  // Sync with the backend: once a run is known, poll its status so the UI
  // reflects runs that complete, fail, or are cancelled elsewhere.
  useEffect(() => {
    if (!activeRunId || !accessToken) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch(
          `${getBackendUrl()}/status/${encodeURIComponent(activeRunId)}`,
          { headers: { Authorization: `Bearer ${accessToken}` } }
        );
        if (cancelled || !res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        if (isTerminalRunStatus(data?.status)) {
          // Do not interrupt an in-flight stop: its own handler settles the phase.
          if (phaseRef.current === "running") {
            updatePhase("idle");
            log(
              `Agent run ${activeRunId.substring(0, 8)} finished with status: ${data.status}.`,
              data.status === "completed" ? "text-emerald-500" : "text-amber-400"
            );
          }
        }
      } catch {
        // Transient network errors: keep the current state and retry next tick.
      }
    };

    poll();
    const interval = setInterval(() => {
      if (phaseRef.current === "running") poll();
    }, RUN_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [activeRunId, accessToken, log, updatePhase]);

  const startStop = useCallback(async () => {
    // Ignore clicks while a start/stop request is already in flight.
    if (phaseRef.current === "starting" || phaseRef.current === "stopping") {
      return;
    }

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

    if (phaseRef.current === "idle") {
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
      updatePhase("starting");
      log(`Triggering AI Agent Loop for ${repoUrl}...`, "text-zinc-500");

      try {
        const res = await fetch(`${getBackendUrl()}/start`, {
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
        if (!res.ok) {
          throw new Error(await readDetail(res));
        }
        const data = await res.json();
        if (!data.run_id) {
          throw new Error("Backend did not return a run identifier");
        }
        setActiveRunId(data.run_id);
        updatePhase("running");
        log(
          `Agent run queued: ${data.run_id.substring(0, 8)}...`,
          "text-emerald-500"
        );
      } catch (err) {
        console.error(err);
        log(`Failed to start the agent run. (${err})`, "text-red-400");
        updatePhase("idle");
      }
      return;
    }

    // phase === "running". A run_id is always known here: the phase only
    // becomes "running" after /start returned one. We never fall back to the
    // body-less /stop, which cancels every run in the organization.
    if (!activeRunId) {
      updatePhase("idle");
      return;
    }

    updatePhase("stopping");
    try {
      const res = await fetch(`${getBackendUrl()}/stop`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: authHeader,
        },
        body: JSON.stringify({ run_id: activeRunId }),
      });
      if (!res.ok) {
        throw new Error(await readDetail(res));
      }
      updatePhase("idle");
      log("Agent Loop Halted.", "text-red-500");
    } catch (err) {
      console.error("Failed to stop agents:", err);
      // The run is still going on the server: stay in "running" so Stop can be retried.
      updatePhase("running");
      log(`Failed to stop the agent run. (${err})`, "text-red-400");
    }
  }, [repoUrl, targetIssue, activeRunId, user, session, log, options, updatePhase]);

  const state: AgentRunState = {
    phase,
    isRunning: phase === "running" || phase === "stopping",
    isPending: phase === "starting" || phase === "stopping",
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
