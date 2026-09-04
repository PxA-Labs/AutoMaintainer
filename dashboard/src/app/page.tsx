/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable @typescript-eslint/no-unused-vars */
/* eslint-disable react-hooks/exhaustive-deps */
"use client";

import { useState, useEffect, useCallback } from "react";
import { BrainCircuit, GitPullRequest, Search, FileCode, CheckCircle, Activity, GitBranch, Settings, Terminal, Play, Square, Code, LogIn, LogOut, User, ChevronDown, Loader2 } from "lucide-react";
import { motion } from "framer-motion";
import WebIDE from "../components/WebIDE";
import dynamic from 'next/dynamic';
import { AuthProvider, useAuth } from '@/lib/auth';
import { getBrowserClient } from '@/lib/supabase';

const supabase = getBrowserClient();

const InteractiveTerminal = dynamic(() => import('../components/InteractiveTerminal'), { ssr: false });

/**
 * Resolves the backend base URL.
 * Priority order:
 *  1. NEXT_PUBLIC_BACKEND_URL env var (e.g. for custom cloud deployments)
 *  2. In local dev (Next.js runs on :3000, FastAPI on :8000) -> use localhost:8000
 *  3. In production (static export served from FastAPI itself) -> use same host
 */
function parseTargetIssue(value: string): number | null {
  const normalized = value.trim().replace(/^#/, "");
  if (!normalized) return null;
  if (!/^\d+$/.test(normalized)) {
    throw new Error("Target issue must be a positive integer, for example 123 or #123.");
  }

  const issueNumber = Number(normalized);
  if (!Number.isSafeInteger(issueNumber) || issueNumber < 1) {
    throw new Error("Target issue must be a positive integer within the safe number range.");
  }
  return issueNumber;
}

function getBackendUrl(): string {
  if (process.env.NEXT_PUBLIC_BACKEND_URL) {
    return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    const port = window.location.port;
    const hostname = window.location.hostname;
    // In local dev (Next.js can run on 3000, 3001, etc. but FastAPI runs on 8000)
    if ((hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") && port !== "8000") {
      const formattedHost = (hostname === "::1" || hostname === "[::1]") ? "[::1]" : hostname;
      return `${window.location.protocol}//${formattedHost}:8000`;
    }
    // Otherwise (production / Docker), same host serves both
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://localhost:8000";
}

function AgentRow({ name, role, icon, status }: { name: string; role: string; icon: React.ReactNode; status: string }) {
  const statusColors: Record<string, string> = {
    active: 'bg-emerald-400',
    idle: 'bg-zinc-500',
    error: 'bg-red-400',
  };
  const statusColor = statusColors[status] || 'bg-zinc-500';
  
  return (
    <div className="flex items-center gap-3 p-2 rounded-lg hover:bg-zinc-800/50 transition-colors">
      <div className="w-8 h-8 rounded-lg bg-zinc-800/50 flex items-center justify-center text-zinc-400">
        {icon}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-zinc-200 truncate">{name}</span>
          <span className={`w-2 h-2 rounded-full ${statusColor} animate-pulse`} />
        </div>
        <span className="text-xs text-zinc-500 truncate block">{role}</span>
      </div>
    </div>
  );
}

function LinkRow({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-3 p-2 rounded-lg hover:bg-zinc-800/50 transition-colors text-sm text-zinc-400">
      {icon}
      <span>{label}</span>
    </div>
  );
}

// User dropdown component
function UserMenu({ user, onSignOut }: { user: any; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  
  return (
    <div className="relative">
      <button 
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 p-2 rounded-lg hover:bg-zinc-800/50 transition-colors"
      >
        <div className="w-8 h-8 rounded-full bg-indigo-500/20 flex items-center justify-center">
          <User className="w-4 h-4 text-indigo-400" />
        </div>
        <div className="hidden sm:block text-left">
          <p className="text-xs font-medium text-zinc-200 truncate max-w-[120px]">
            {user?.user_metadata?.full_name || user?.email || 'User'}
          </p>
          <p className="text-[10px] text-zinc-500 truncate max-w-[120px]">
            {user?.email}
          </p>
        </div>
        <ChevronDown className="w-4 h-4 text-zinc-500" />
      </button>
      
      {open && (
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          className="absolute right-0 top-full mt-2 w-56 bg-zinc-900 border border-zinc-800 rounded-lg shadow-lg py-2 z-50"
        >
          <div className="px-4 py-2 border-b border-zinc-800">
            <p className="text-xs font-medium text-zinc-200 truncate">
              {user?.user_metadata?.full_name || user?.email || 'User'}
            </p>
            <p className="text-[10px] text-zinc-500 truncate">
              {user?.email}
            </p>
          </div>
          <button
            onClick={onSignOut}
            className="w-full flex items-center gap-2 px-4 py-2 text-sm text-red-400 hover:bg-zinc-800/50 transition-colors"
          >
            <LogOut className="w-4 h-4" />
            Sign Out
          </button>
        </motion.div>
      )}
    </div>
  );
}

// Main dashboard component (wrapped with auth)
function DashboardContent() {
  const { user, session, loading: authLoading, signInWithGitHub, signOut } = useAuth();
  const [skipAuth, setSkipAuth] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [repoUrl, setRepoUrl] = useState("owner/repo");
  const [isEditingRepo, setIsEditingRepo] = useState(false);
  const [targetIssue, setTargetIssue] = useState("");
  const [activeTab, setActiveTab] = useState("dashboard");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [terminalMode, setTerminalMode] = useState<'logs' | 'pty'>('logs');
  const [logs, setLogs] = useState([
    { time: "00:00:00", agent: "System", msg: "Connecting to backend...", color: "text-zinc-500" }
  ]);
  const [systemHealth, setSystemHealth] = useState({ latency: 0, tokensUsed: 0 });
  const [pipeline, setPipeline] = useState<any[]>([]);
  const [activity, setActivity] = useState<any[]>([]);
  const [agentStatus, setAgentStatus] = useState<any>({
    Architect: 'idle',
    Visionary: 'idle',
    Reviewer: 'idle',
    Implementer: 'idle',
    Maintainer: 'idle',
  });
  const [isSupabaseUnreachable, setIsSupabaseUnreachable] = useState(false);
  const [runs, setRuns] = useState<any[]>([]);
  const [loadingRuns, setLoadingRuns] = useState(false);

  // Clear sensitive state if the user signs out
  useEffect(() => {
    if (!user) {
      setActiveRunId(null);
      setIsRunning(false);
      setLogs([{ time: "00:00:00", agent: "System", msg: "Connecting to backend...", color: "text-zinc-500" }]);
      setPipeline([]);
      setActivity([]);
      setRuns([]);
      // Reset skipAuth so they don't immediately get dumped back into a broken state if not skipping
    }
  }, [user]);

  const handleStartStop = useCallback(async () => {
    if (!user) {
      setSkipAuth(false);
      return;
    }
    
    if (!isRunning) {
      if (!repoUrl || repoUrl.trim() === "owner/repo" || repoUrl.trim() === "") {
        alert("Please enter a Target Repository first!");
        setIsEditingRepo(true);
        setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: "Please configure a valid Target Repository in the sidebar first.", color: "text-red-400" }]);
        return;
      }
      let targetIssueNumber: number | null;
      try {
        targetIssueNumber = parseTargetIssue(targetIssue);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Invalid target issue.";
        alert(message);
        setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: message, color: "text-red-400" }]);
        return;
      }

      setIsRunning(true);
      setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: `Triggering AI Agent Loop for ${repoUrl}...`, color: "text-zinc-500" }]);
      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/start`, {
          method: "POST",
          headers: { 
            "Content-Type": "application/json",
            "Authorization": session?.access_token ? `Bearer ${session.access_token}` : ""
          },
          body: JSON.stringify({
            repo_name: repoUrl,
            target_issue: targetIssueNumber,
          })
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = typeof data.detail === "string" ? data.detail : `Backend returned HTTP ${res.status}.`;
          throw new Error(detail);
        }
        if (data.run_id) {
          setActiveRunId(data.run_id);
          setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: `Agent run queued: ${data.run_id.substring(0,8)}...`, color: "text-emerald-500" }]);
        }
      } catch (err) {
        console.error(err);
        setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: `Failed to reach backend. Is it running? (${err})`, color: "text-red-400" }]);
        setIsRunning(false);
      }
    } else {
      setIsRunning(false);
      setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: "Agent Loop Halted.", color: "text-red-500" }]);
      try {
        const backendUrl = getBackendUrl();
        if (activeRunId) {
          await fetch(`${backendUrl}/stop`, { 
            method: "POST",
            headers: { 
              "Content-Type": "application/json",
              "Authorization": session?.access_token ? `Bearer ${session.access_token}` : ""
            },
            body: JSON.stringify({ run_id: activeRunId })
          });
        } else {
          await fetch(`${backendUrl}/stop`, { 
            method: "POST",
            headers: {
              "Authorization": session?.access_token ? `Bearer ${session.access_token}` : ""
            }
          });
        }
      } catch (err) {
        console.error("Failed to stop agents:", err);
      }
    }
  }, [isRunning, repoUrl, targetIssue, activeRunId]);

  const fetchRuns = useCallback(async () => {
    if (!session) return;
    setLoadingRuns(true);
    try {
      const backendUrl = getBackendUrl();
      const res = await fetch(`${backendUrl}/runs`, {
        headers: { "Authorization": `Bearer ${session.access_token}` }
      });
      const data = await res.json();
      setRuns(data.runs || []);
    } catch (err) {
      console.error("Failed to fetch runs:", err);
    } finally {
      setLoadingRuns(false);
    }
  }, [session]);

  // Fetch runs on mount and when session changes
  useEffect(() => {
    if (session) {
      fetchRuns();
    }
  }, [session, fetchRuns]);

  useEffect(() => {
    if (!activeRunId) return;

    let isMounted = true;
    setSystemHealth({ latency: 0, tokensUsed: 0 });
    setLogs([{ time: new Date().toLocaleTimeString(), agent: "System", msg: "Connecting...", color: "text-zinc-500" }]);
    setPipeline([]);
    setActivity([]);
    setAgentStatus({
      Architect: 'idle',
      Visionary: 'idle',
      Reviewer: 'idle',
      Implementer: 'idle',
      Maintainer: 'idle',
    });

    let isReplaying = true;
    const realtimeBuffer: any[] = [];
    const processedLogIds = new Set<string>();

    const processLogRow = (
      row: any,
      accumulator?: { historyLogs: any[]; historyTokens: number; lastLatency: number | null }
    ) => {
      if (!isMounted) return;
      if (row.id && processedLogIds.has(row.id)) return;
      if (row.id) processedLogIds.add(row.id);

      if (row.log_type === 'ui_update') {
        const msgData = row.metadata || {};
        if (msgData.systemHealth) {
          if (accumulator) {
            accumulator.historyTokens += msgData.systemHealth.tokensUsed || 0;
            if (msgData.systemHealth.latency !== undefined && msgData.systemHealth.latency !== null) {
              accumulator.lastLatency = msgData.systemHealth.latency;
            }
          } else {
            setSystemHealth((prev) => ({
              latency: msgData.systemHealth.latency ?? prev.latency,
              tokensUsed: prev.tokensUsed + (msgData.systemHealth.tokensUsed || 0)
            }));
          }
        }
        if (msgData.agentStatus) setAgentStatus((prev: any) => ({ ...prev, ...msgData.agentStatus }));
        if (msgData.pipeline) {
          setPipeline((prev) => {
            const exists = prev.find((p) => p.id === msgData.pipeline.id);
            if (exists) return prev.map((p) => (p.id === msgData.pipeline.id ? msgData.pipeline : p));
            return [msgData.pipeline, ...prev];
          });
        }
        if (msgData.activity) setActivity((prev) => [msgData.activity, ...prev]);
      } else {
        const date = new Date(row.created_at || Date.now());
        const logEntry = {
          time: date.toLocaleTimeString(),
          agent: row.agent_name || "System",
          msg: row.message || "",
          color: row.color || "text-zinc-400"
        };
        if (accumulator) {
          accumulator.historyLogs.push(logEntry);
        } else {
          setLogs((prev) => [...prev, logEntry]);
        }
      }
    };

    // Fetch existing historical logs from Supabase
    const fetchHistory = async () => {
      try {
        const { data, error } = await supabase
          .from('logs')
          .select('*')
          .eq('run_id', activeRunId)
          .order('created_at', { ascending: true });

        if (!isMounted) return;

        if (error) {
          console.error("Error fetching historical logs:", error);
          setLogs((prev) => [
            ...prev,
            {
              time: new Date().toLocaleTimeString(),
              agent: "System",
              msg: `Failed to load historical logs: ${error.message || String(error)}`,
              color: "text-red-400"
            }
          ]);
          return;
        }

        if (data && data.length > 0) {
          const accumulator = {
            historyLogs: [] as any[],
            historyTokens: 0,
            lastLatency: null as number | null
          };

          data.forEach((row: any) => {
            processLogRow(row, accumulator);
          });

          if (!isMounted) return;

          if (accumulator.historyTokens > 0 || accumulator.lastLatency !== null) {
            setSystemHealth((prev) => ({
              latency: accumulator.lastLatency ?? prev.latency,
              tokensUsed: accumulator.historyTokens
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
              color: "text-red-400"
            }
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

    // Subscribe to new real-time logs
    const channel = supabase
      .channel(`logs_${activeRunId}`)
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'logs', filter: `run_id=eq.${activeRunId}` },
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
        if (status === 'SUBSCRIBED') {
          console.log(`Subscribed to Supabase Realtime for run ${activeRunId}`);
        }
      });

    return () => {
      isMounted = false;
      supabase.removeChannel(channel);
    };
  }, [activeRunId]);

  useEffect(() => {
    let active = true;
    let timeoutId: NodeJS.Timeout | null = null;
    let currentController: AbortController | null = null;

    const checkSupabaseHealth = async () => {
      if (!active) return;

      if (currentController) {
        currentController.abort();
      }

      const controller = new AbortController();
      currentController = controller;

      const timeoutPromise = new Promise((_, reject) => {
        timeoutId = setTimeout(() => {
          controller.abort();
          reject(new Error("Request timeout"));
        }, 8000);
      });

      try {
        const backendUrl = getBackendUrl();
        const fetchPromise = fetch(`${backendUrl}/healthz/supabase`, {
          signal: controller.signal
        });

        const res = await Promise.race([fetchPromise, timeoutPromise]) as Response;
        clearTimeout(timeoutId!);

        if (!res.ok) {
          setIsSupabaseUnreachable(true);
        } else {
          setIsSupabaseUnreachable(false);
        }
      } catch (err: any) {
        if (err.name !== "AbortError") {
          console.error("Failed to check Supabase health:", err);
          setIsSupabaseUnreachable(true);
        }
      } finally {
        if (active) {
          timeoutId = setTimeout(checkSupabaseHealth, 15000);
        }
      }
    };

    checkSupabaseHealth();

    return () => {
      active = false;
      if (currentController) {
        currentController.abort();
      }
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, []);

  // Show auth gate if not authenticated
  if (authLoading) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
      </div>
    );
  }

  if (!user && !skipAuth) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <div className="text-center p-8">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center mx-auto mb-6 shadow-lg shadow-purple-500/20">
            <BrainCircuit className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-zinc-100 to-zinc-400 mb-2">
            AutoMaintainer
          </h1>
          <p className="text-zinc-500 mb-8 max-w-md mx-auto">
            An Always-On Autonomous AI Software Engineering Team. Sign in with GitHub to start managing your repositories.
          </p>
          <div className="flex flex-col items-center gap-4">
            <button
              onClick={signInWithGitHub}
              disabled={authLoading}
              className="inline-flex items-center gap-2 px-6 py-3 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-lg font-medium hover:bg-indigo-500/20 transition-colors"
            >
              <LogIn className="w-5 h-5" />
              Sign in with GitHub
            </button>
            <button
              onClick={() => setSkipAuth(true)}
              className="text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
            >
              Skip for now
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-full bg-[#0a0a0a] text-zinc-100 font-sans overflow-hidden">
      {/* Sidebar Panel */}
      <div className="w-64 border-r border-zinc-800/50 bg-[#0d0d0d] flex flex-col">
        <div className="p-6 border-b border-zinc-800/50 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-purple-500/20">
            <BrainCircuit className="w-5 h-5 text-white" />
          </div>
          <h1 className="font-bold text-lg tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-100 to-zinc-400">
            AutoMaintainer
          </h1>
        </div>
        
        <div className="flex-1 py-6 px-4 space-y-8 overflow-y-auto">
          {/* User Info */}
          <div className="p-3 bg-zinc-800/30 rounded-lg border border-zinc-800/50">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-indigo-500/20 flex items-center justify-center">
                <User className="w-4 h-4 text-indigo-400" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium text-zinc-200 truncate">
                  {user?.user_metadata?.full_name || user?.email || 'User'}
                </p>
                <p className="text-[10px] text-zinc-500 truncate">
                  {user?.email}
                </p>
              </div>
            </div>
          </div>

          {/* Agent Roster */}
          <div>
            <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-4 px-2">The Crew</h2>
            <div className="space-y-1">
              <AgentRow name="Architect" role="Principal Engineer" icon={<BrainCircuit className="w-4 h-4 text-rose-400" />} status={agentStatus.Architect} />
              <AgentRow name="Visionary" role="Brainstormer" icon={<Search className="w-4 h-4 text-emerald-400" />} status={agentStatus.Visionary} />
              <AgentRow name="Reviewer" role="Product Manager" icon={<CheckCircle className="w-4 h-4 text-amber-400" />} status={agentStatus.Reviewer} />
              <AgentRow name="Implementer" role="Developer" icon={<FileCode className="w-4 h-4 text-blue-400" />} status={agentStatus.Implementer} />
              <AgentRow name="Maintainer" role="Senior Engineer" icon={<GitPullRequest className="w-4 h-4 text-purple-400" />} status={agentStatus.Maintainer} />
            </div>
          </div>
          
          {/* Settings / Links */}
          <div>
            <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-4 px-2">Configuration</h2>
            <div className="space-y-1">
              <div className="flex flex-col p-2 rounded-lg hover:bg-zinc-800/50 transition-colors text-sm gap-2">
                <div className="flex items-center justify-between cursor-pointer" onClick={() => {
                  if (!user) {
                    setSkipAuth(false);
                    return;
                  }
                  setIsEditingRepo(true);
                }}>
                  <div className="flex items-center gap-3 text-zinc-400">
                    <GitBranch className="w-4 h-4 text-zinc-400" />
                    <span>Target Repository</span>
                  </div>
                  {!isEditingRepo && <span className="text-xs font-mono text-zinc-500 truncate max-w-[100px] hover:text-white transition-colors">{repoUrl}</span>}
                </div>
                {isEditingRepo && (
                  <input 
                    type="text" 
                    value={repoUrl}
                    onChange={(e) => setRepoUrl(e.target.value)}
                    onBlur={() => setIsEditingRepo(false)}
                    onKeyDown={(e) => e.key === 'Enter' && setIsEditingRepo(false)}
                    className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-indigo-500"
                    placeholder="e.g. facebook/react"
                    autoFocus
                  />
                )}
              </div>
              <div className="flex flex-col p-2 rounded-lg hover:bg-zinc-800/50 transition-colors text-sm gap-2">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3 text-zinc-400">
                      <Search className="w-4 h-4 text-zinc-400" />
                      <span>Target Issue</span>
                    </div>
                  </div>
                  <input 
                    type="text" 
                    value={targetIssue}
                    onChange={(e) => setTargetIssue(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-indigo-500"
                    placeholder="Issue number (e.g. 1)"
                  />
              </div>
             
              <div className="flex flex-col p-2 rounded-lg hover:bg-zinc-800/50 transition-colors text-sm gap-2">
                  <div className="grid grid-cols-2 gap-2">
                    <button 
                      onClick={() => setActiveTab(activeTab === 'gitnexus' ? 'dashboard' : 'gitnexus')}
                      className={`w-full py-2 rounded-md border transition-all font-medium flex items-center justify-center gap-2 shadow-lg ${activeTab === 'gitnexus' ? 'bg-indigo-500 text-white border-indigo-400' : 'bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500/20 border-indigo-500/20'}`}
                    >
                      <FileCode className="w-4 h-4" />
                      Code Graph
                    </button>
                    <button 
                      onClick={() => setActiveTab(activeTab === 'ide' ? 'dashboard' : 'ide')}
                      className={`w-full py-2 rounded-md border transition-all font-medium flex items-center justify-center gap-2 shadow-lg ${activeTab === 'ide' ? 'bg-blue-500 text-white border-blue-400' : 'bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 border-blue-500/20'}`}
                    >
                      <Code className="w-4 h-4" />
                      Web IDE
                    </button>
                  </div>
              </div>

              <button
                onClick={() => setActiveTab(activeTab === 'runs' ? 'dashboard' : 'runs')}
                className={`w-full py-2 rounded-md border transition-all font-medium flex items-center justify-center gap-2 shadow-lg ${activeTab === 'runs' ? 'bg-emerald-500 text-white border-emerald-400' : 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 border-emerald-500/20'}`}
              >
                <Activity className="w-4 h-4" />
                Run History
              </button>
              
              <LinkRow icon={<Settings className="w-4 h-4 text-zinc-400" />} label="Settings" />
            </div>
          </div>
        </div>
        
        {/* System Status Footer */}
        <div className="p-4 border-t border-zinc-800/50">
          <UserMenu user={user} onSignOut={signOut} />
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-screen overflow-hidden relative bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-zinc-900/40 via-[#0a0a0a] to-[#0a0a0a]">
        {isSupabaseUnreachable && (
          <div role="alert" className="bg-red-500/10 border-b border-red-500/20 px-8 py-3 flex items-center gap-3 text-red-400 text-sm font-medium transition-all shrink-0">
            <span className="flex h-2 w-2 relative shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-red-500"></span>
            </span>
            <span>Supabase Database is unreachable or paused. Historical logs and persistence are currently disabled.</span>
          </div>
        )}
       
        {/* Top Header */}
        <header className="h-16 border-b border-zinc-800/50 flex items-center justify-between px-8 backdrop-blur-sm">
          <div className="flex items-center gap-3 text-sm text-zinc-400">
            <span className="flex h-2 w-2 relative">
              <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${isRunning ? 'bg-emerald-400' : 'bg-red-400'}`}></span>
              <span className={`relative inline-flex rounded-full h-2 w-2 ${isRunning ? 'bg-emerald-500' : 'bg-red-500'}`}></span>
            </span>
            {isRunning ? 'System Active • Monitoring Repository' : 'System Halted'}
          </div>
          <div className="flex items-center gap-4">
            <div className="text-xs font-mono text-zinc-500 bg-zinc-900/50 px-3 py-1.5 rounded-md border border-zinc-800/50">
              Model: Llama-3-70b (Cloud)
            </div>
            {systemHealth.tokensUsed > 0 && (
              <div className="text-xs font-mono text-zinc-500 bg-zinc-900/50 px-3 py-1.5 rounded-md border border-zinc-800/50">
                Tokens: {systemHealth.tokensUsed.toLocaleString()} • Latency: {systemHealth.latency}ms
              </div>
            )}
          </div>
        </header>

          {/* Dashboard Grid, GitNexus, or IDE */}
          {activeTab === 'ide' ? (
            <main className="flex-1 flex flex-col overflow-hidden">
              <div className="flex-1 min-h-0">
                {user ? (
                  <WebIDE repoUrl={repoUrl} />
                ) : (
                  <div className="flex items-center justify-center h-full bg-[#1e1e1e] text-zinc-500">
                    Sign in to access the Web IDE
                  </div>
                )}
              </div>
              <div className="h-48 border-t border-[#333333] bg-[#1e1e1e] flex flex-col shrink-0">
                  <div className="h-8 bg-[#252526] flex items-center px-4 gap-4 shrink-0 shadow-sm border-b border-[#333333]">
                    <button onClick={() => setTerminalMode('logs')} className={`flex items-center gap-2 text-xs font-mono transition-colors ${terminalMode === 'logs' ? 'text-zinc-300' : 'text-zinc-500 hover:text-zinc-400'}`}>
                      <Terminal className="w-3.5 h-3.5" />
                      agent_execution_log.sh
                    </button>
                    <div className="w-px h-3 bg-zinc-700"></div>
                    <button onClick={() => setTerminalMode('pty')} className={`flex items-center gap-2 text-xs font-mono transition-colors ${terminalMode === 'pty' ? 'text-indigo-400' : 'text-zinc-500 hover:text-zinc-400'}`}>
                      <Terminal className="w-3.5 h-3.5" />
                      interactive_shell.pty
                    </button>
                  </div>
                  {terminalMode === 'logs' ? (
                    <div className="flex-1 min-h-0 p-4 overflow-y-auto font-mono text-xs bg-[#1e1e1e]">
                      {logs.map((log, i) => (
                        <div key={i} className={`flex gap-2 py-1 ${log.color}`}>
                          <span className="text-zinc-500 shrink-0">{log.time}</span>
                          <span className="text-zinc-400 shrink-0">[{log.agent}]</span>
                          <span>{log.msg}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="flex-1 min-h-0">
                      {user ? (
                        <InteractiveTerminal repoUrl={repoUrl} />
                      ) : (
                        <div className="flex items-center justify-center h-full text-zinc-500 text-sm">
                          Sign in to access the Interactive Terminal
                        </div>
                      )}
                    </div>
                  )}
              </div>
            </main>
          ) : activeTab === 'runs' ? (
            <main className="flex-1 flex flex-col overflow-hidden p-6">
              <div className="flex items-center justify-between mb-6">
                <h2 className="text-xl font-bold">Run History</h2>
                <button 
                  onClick={fetchRuns}
                  disabled={loadingRuns}
                  className="flex items-center gap-2 px-4 py-2 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-lg font-medium hover:bg-indigo-500/20 transition-colors"
                >
                  <Loader2 className={`w-4 h-4 ${loadingRuns ? 'animate-spin' : ''}`} />
                  Refresh
                </button>
              </div>
              <div className="flex-1 overflow-y-auto">
                {loadingRuns ? (
                  <div className="flex items-center justify-center h-64">
                    <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
                  </div>
                ) : runs.length === 0 ? (
                  <div className="flex flex-col items-center justify-center h-64 text-zinc-500">
                    <Activity className="w-12 h-12 mb-4 text-zinc-700" />
                    <p className="text-lg">No runs yet</p>
                    <p className="text-sm">Start an agent run to see history here</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {runs.map((run) => (
                      <div key={run.id} className="bg-zinc-900/50 border border-zinc-800/50 rounded-lg p-4 hover:border-zinc-700/50 transition-colors">
                        <div className="flex items-center justify-between mb-2">
                          <div className="flex items-center gap-3">
                            <span className="font-mono text-xs text-zinc-400">{run.id.substring(0,8)}...</span>
                            <span className={`px-2 py-1 rounded text-xs font-medium ${
                              run.status === 'running' ? 'bg-emerald-500/20 text-emerald-400' :
                              run.status === 'completed' ? 'bg-blue-500/20 text-blue-400' :
                              run.status === 'failed' ? 'bg-red-500/20 text-red-400' :
                              run.status === 'cancelled' ? 'bg-amber-500/20 text-amber-400' :
                              'bg-zinc-500/20 text-zinc-400'
                            }`}>
                              {run.status}
                            </span>
                            <span className="text-xs text-zinc-500">{run.repo_name}</span>
                          </div>
                          <span className="text-xs text-zinc-500">
                            {new Date(run.created_at).toLocaleString()}
                          </span>
                        </div>
                        {run.error_message && (
                          <div className="text-xs text-red-400 bg-red-500/10 p-2 rounded mt-2">
                            {run.error_message}
                          </div>
                        )}
                        {run.result_summary && (
                          <div className="text-xs text-zinc-300 mt-2">
                            {run.result_summary}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </main>
          ) : (
            <main className="flex-1 flex flex-col overflow-hidden p-6">
              {/* Dashboard Overview */}
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
                {/* Pipeline */}
                <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl p-4">
                  <h3 className="font-medium text-zinc-300 mb-3 flex items-center gap-2">
                    <GitPullRequest className="w-4 h-4 text-indigo-400" />
                    Pipeline
                  </h3>
                  <div className="space-y-2">
                    {pipeline.length > 0 ? (
                      pipeline.map((p: any) => (
                        <div key={p.id} className="flex items-center gap-2 p-2 bg-zinc-800/50 rounded">
                          <span className="text-xs font-mono text-zinc-400">{p.id}</span>
                          <span className="text-xs text-zinc-300 truncate flex-1">{p.title}</span>
                          <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                            p.status === 'architecting' ? 'bg-rose-500/20 text-rose-400' :
                            p.status === 'ideating' ? 'bg-emerald-500/20 text-emerald-400' :
                            p.status === 'reviewing' ? 'bg-amber-500/20 text-amber-400' :
                            p.status === 'implementing' ? 'bg-blue-500/20 text-blue-400' :
                            p.status === 'maintaining' ? 'bg-purple-500/20 text-purple-400' :
                            'bg-zinc-500/20 text-zinc-400'
                          }`}>
                            {p.status}
                          </span>
                        </div>
                      ))
                    ) : (
                      <p className="text-zinc-500 text-sm text-center py-4">No active pipeline</p>
                    )}
                  </div>
                </div>

                {/* Activity Feed */}
                <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl p-4">
                  <h3 className="font-medium text-zinc-300 mb-3 flex items-center gap-2">
                    <Activity className="w-4 h-4 text-emerald-400" />
                    Recent Activity
                  </h3>
                  <div className="space-y-2 max-h-64 overflow-y-auto">
                    {activity.length > 0 ? (
                      activity.slice(0, 10).map((a: any, i: number) => (
                        <div key={i} className="flex items-start gap-2 p-2 bg-zinc-800/50 rounded">
                          <span className="text-[10px] text-zinc-500 shrink-0 mt-0.5">{a.time || 'now'}</span>
                          <span className="text-xs text-zinc-300">{a.title}</span>
                        </div>
                      ))
                    ) : (
                      <p className="text-zinc-500 text-sm text-center py-4">No recent activity</p>
                    )}
                  </div>
                </div>

                {/* System Health */}
                <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl p-4">
                  <h3 className="font-medium text-zinc-300 mb-3 flex items-center gap-2">
                    <CheckCircle className="w-4 h-4 text-emerald-400" />
                    System Health
                  </h3>
                  <div className="space-y-3">
                    <div>
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-zinc-400">Latency</span>
                        <span className="font-mono text-zinc-200">{systemHealth.latency}ms</span>
                      </div>
                      <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
                        <div 
                          className={`h-full transition-all ${
                            systemHealth.latency < 1000 ? 'bg-emerald-400' :
                            systemHealth.latency < 3000 ? 'bg-amber-400' :
                            'bg-red-400'
                          }`}
                          style={{ width: `${Math.min(systemHealth.latency / 5000 * 100, 100)}%` }}
                        />
                      </div>
                    </div>
                    <div>
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-zinc-400">Tokens Used</span>
                        <span className="font-mono text-zinc-200">{systemHealth.tokensUsed.toLocaleString()}</span>
                      </div>
                      <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-blue-400 transition-all"
                          style={{ width: `${Math.min(systemHealth.tokensUsed / 50000 * 100, 100)}%` }}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* Agent Status Grid */}
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
                {[
                  { name: 'Architect', role: 'Principal Engineer', icon: <BrainCircuit className="w-4 h-4" />, color: 'text-rose-400', bgColor: 'bg-rose-500/20', status: agentStatus.Architect },
                  { name: 'Visionary', role: 'Brainstormer', icon: <Search className="w-4 h-4" />, color: 'text-emerald-400', bgColor: 'bg-emerald-500/20', status: agentStatus.Visionary },
                  { name: 'Reviewer', role: 'Product Manager', icon: <CheckCircle className="w-4 h-4" />, color: 'text-amber-400', bgColor: 'bg-amber-500/20', status: agentStatus.Reviewer },
                  { name: 'Implementer', role: 'Developer', icon: <FileCode className="w-4 h-4" />, color: 'text-blue-400', bgColor: 'bg-blue-500/20', status: agentStatus.Implementer },
                  { name: 'Maintainer', role: 'Senior Engineer', icon: <GitPullRequest className="w-4 h-4" />, color: 'text-purple-400', bgColor: 'bg-purple-500/20', status: agentStatus.Maintainer },
                ].map((agent) => (
                  <div key={agent.name} className={`bg-zinc-900/50 border border-zinc-800/50 rounded-xl p-4 transition-all ${
                    agent.status === 'active' ? 'border-indigo-500/50 shadow-lg shadow-indigo-500/10' : ''
                  }`}>
                    <div className="flex items-center gap-3 mb-3">
                      <div className={`w-10 h-10 rounded-lg ${agent.bgColor} flex items-center justify-center`}>
                        <span className={agent.color}>{agent.icon}</span>
                      </div>
                      <div>
                        <p className="font-medium text-zinc-100">{agent.name}</p>
                        <p className="text-xs text-zinc-500">{agent.role}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`w-2.5 h-2.5 rounded-full ${agent.status === 'active' ? 'bg-emerald-400 animate-pulse' : 'bg-zinc-500'}`} />
                      <span className="text-xs font-medium text-zinc-400 capitalize">{agent.status}</span>
                    </div>
                  </div>
                ))}
              </div>

              {/* Logs */}
              <div className="mt-6 flex-1 min-h-0">
                <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-zinc-800/50 flex items-center justify-between">
                    <h3 className="font-medium text-zinc-300 flex items-center gap-2">
                      <Terminal className="w-4 h-4" />
                      Agent Execution Log
                    </h3>
                    <span className="text-xs text-zinc-500">{logs.length} entries</span>
                  </div>
                  <div className="h-96 overflow-y-auto p-4 font-mono text-xs bg-[#1e1e1e]">
                    {logs.map((log, i) => (
                      <div key={i} className={`flex gap-2 py-1 ${log.color}`}>
                        <span className="text-zinc-500 shrink-0">{log.time}</span>
                        <span className="text-zinc-400 shrink-0">[{log.agent}]</span>
                        <span>{log.msg}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </main>
          )}
        </div>
      </div>
    );
}

// Export the main component wrapped with AuthProvider
export default function Home() {
  return (
    <AuthProvider>
      <DashboardContent />
    </AuthProvider>
  );
}