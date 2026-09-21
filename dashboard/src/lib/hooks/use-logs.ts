"use client";

import { useState, useEffect, useCallback } from "react";
import { getBrowserClient } from "@/lib/supabase";
import type { LogEntry } from "./use-agent-run";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface LogsState {
  logs: LogEntry[];
  systemHealth: { latency: number; tokensUsed: number };
  pipeline: any[];
  activity: any[];
  agentStatus: Record<string, string>;
}

const DEFAULT_AGENT_STATUS: Record<string, string> = {
  Architect: "idle",
  Visionary: "idle",
  Reviewer: "idle",
  Implementer: "idle",
  Maintainer: "idle",
};

const INITIAL_LOG: LogEntry = {
  time: "00:00:00",
  agent: "System",
  msg: "Connecting to backend...",
  color: "text-zinc-500",
};

export function useLogs(activeRunId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([INITIAL_LOG]);
  const [systemHealth, setSystemHealth] = useState({
    latency: 0,
    tokensUsed: 0,
  });
  const [pipeline, setPipeline] = useState<any[]>([]);
  const [activity, setActivity] = useState<any[]>([]);
  const [agentStatus, setAgentStatus] =
    useState<Record<string, string>>(DEFAULT_AGENT_STATUS);

  const addLog = useCallback((entry: LogEntry) => {
    setLogs((prev) => [...prev, entry]);
  }, []);

  const resetState = useCallback(() => {
    setLogs([INITIAL_LOG]);
    setPipeline([]);
    setActivity([]);
    setAgentStatus(DEFAULT_AGENT_STATUS);
    setSystemHealth({ latency: 0, tokensUsed: 0 });
  }, []);

  // Main realtime log subscription + history replay
  useEffect(() => {
    if (!activeRunId) return;

    const supabase = getBrowserClient();
    let isMounted = true;
    setSystemHealth({ latency: 0, tokensUsed: 0 });
    setLogs([
      {
        time: new Date().toLocaleTimeString(),
        agent: "System",
        msg: "Connecting...",
        color: "text-zinc-500",
      },
    ]);
    setPipeline([]);
    setActivity([]);
    setAgentStatus({ ...DEFAULT_AGENT_STATUS });

    let isReplaying = true;
    const realtimeBuffer: any[] = [];
    const processedLogIds = new Set<string>();

    const processLogRow = (
      row: any,
      accumulator?: {
        historyLogs: LogEntry[];
        historyTokens: number;
        lastLatency: number | null;
      }
    ) => {
      if (!isMounted) return;
      if (row.id && processedLogIds.has(row.id)) return;
      if (row.id) processedLogIds.add(row.id);

      if (row.log_type === "ui_update") {
        const msgData = row.metadata || {};
        if (msgData.systemHealth) {
          if (accumulator) {
            accumulator.historyTokens +=
              msgData.systemHealth.tokensUsed || 0;
            if (
              msgData.systemHealth.latency !== undefined &&
              msgData.systemHealth.latency !== null
            ) {
              accumulator.lastLatency = msgData.systemHealth.latency;
            }
          } else {
            setSystemHealth((prev) => ({
              latency: msgData.systemHealth.latency ?? prev.latency,
              tokensUsed:
                prev.tokensUsed + (msgData.systemHealth.tokensUsed || 0),
            }));
          }
        }
        if (msgData.agentStatus)
          setAgentStatus((prev) => ({ ...prev, ...msgData.agentStatus }));
        if (msgData.pipeline) {
          setPipeline((prev) => {
            const exists = prev.find((p) => p.id === msgData.pipeline.id);
            if (exists)
              return prev.map((p) =>
                p.id === msgData.pipeline.id ? msgData.pipeline : p
              );
            return [msgData.pipeline, ...prev];
          });
        }
        if (msgData.activity)
          setActivity((prev) => [msgData.activity, ...prev]);
      } else {
        const date = new Date(row.created_at || Date.now());
        const logEntry: LogEntry = {
          time: date.toLocaleTimeString(),
          agent: row.agent_name || "System",
          msg: row.message || "",
          color: row.color || "text-zinc-400",
        };
        if (accumulator) {
          accumulator.historyLogs.push(logEntry);
        } else {
          setLogs((prev) => [...prev, logEntry]);
        }
      }
    };

    const fetchHistory = async () => {
      try {
        const { data, error } = await supabase
          .from("logs")
          .select("*")
          .eq("run_id", activeRunId)
          .order("created_at", { ascending: true });

        if (!isMounted) return;

        if (error) {
          console.error("Error fetching historical logs:", error);
          setLogs((prev) => [
            ...prev,
            {
              time: new Date().toLocaleTimeString(),
              agent: "System",
              msg: `Failed to load historical logs: ${error.message || String(error)}`,
              color: "text-red-400",
            },
          ]);
          return;
        }

        if (data && data.length > 0) {
          const accumulator = {
            historyLogs: [] as LogEntry[],
            historyTokens: 0,
            lastLatency: null as number | null,
          };

          data.forEach((row: any) => {
            processLogRow(row, accumulator);
          });

          if (!isMounted) return;

          if (
            accumulator.historyTokens > 0 ||
            accumulator.lastLatency !== null
          ) {
            setSystemHealth((prev) => ({
              latency: accumulator.lastLatency ?? prev.latency,
              tokensUsed: accumulator.historyTokens,
            }));
          }

          if (accumulator.historyLogs.length > 0) {
            setLogs((prev) => [...prev, ...accumulator.historyLogs]);
          }
        }
      } catch (err) {
        if (isMounted) {
          console.error("Error fetching historical logs:", err);
          setLogs((prev) => [
            ...prev,
            {
              time: new Date().toLocaleTimeString(),
              agent: "System",
              msg: `Error loading historical logs: ${err instanceof Error ? err.message : String(err)}`,
              color: "text-red-400",
            },
          ]);
        }
      } finally {
        if (isMounted) {
          isReplaying = false;
          realtimeBuffer.forEach((row) => processLogRow(row));
          realtimeBuffer.length = 0;
        }
      }
    };
    fetchHistory();

    const channel = supabase
      .channel(`logs_${activeRunId}`)
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "logs",
          filter: `run_id=eq.${activeRunId}`,
        },
        (payload) => {
          if (!isMounted) return;
          const row = payload.new as any;
          if (isReplaying) {
            realtimeBuffer.push(row);
          } else {
            processLogRow(row);
          }
        }
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          console.log(
            `Subscribed to Supabase Realtime for run ${activeRunId}`
          );
        }
      });

    return () => {
      isMounted = false;
      supabase.removeChannel(channel);
    };
  }, [activeRunId]);

  const state: LogsState = {
    logs,
    systemHealth,
    pipeline,
    activity,
    agentStatus,
  };

  return { ...state, addLog, resetState };
}
