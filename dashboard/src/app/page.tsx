/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useEffect } from "react";
import { BrainCircuit, GitPullRequest, Search, FileCode, CheckCircle, Activity, GitBranch, Settings, Terminal, Play, Square, Code } from "lucide-react";
import { motion } from "framer-motion";
import WebIDE from "../components/WebIDE";
import dynamic from 'next/dynamic';

const InteractiveTerminal = dynamic(() => import('../components/InteractiveTerminal'), { ssr: false });

/**
 * Resolves the backend base URL.
 *
 * Priority order:
 *  1. NEXT_PUBLIC_BACKEND_URL env var (e.g. for custom cloud deployments)
 *  2. In local dev (Next.js runs on :3000, FastAPI on :8000) -> use localhost:8000
 *  3. In production (static export served from FastAPI itself) -> use same host
 */
function getBackendUrl(): string {
  if (process.env.NEXT_PUBLIC_BACKEND_URL) {
    return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    const port = window.location.port;
    const hostname = window.location.hostname;
    // If frontend is on the standard Next.js dev port, point to the FastAPI port
    if (port === "3000") {
      return `${window.location.protocol}//${hostname}:8000`;
    }
    // Otherwise (production / Docker), same host serves both
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://localhost:8000";
}

export default function Home() {
  const [isRunning, setIsRunning] = useState(false);
  const [repoUrl, setRepoUrl] = useState("owner/repo");
  const [isEditingRepo, setIsEditingRepo] = useState(false);
  const [targetIssue, setTargetIssue] = useState("");
  const [activeTab, setActiveTab] = useState("dashboard");
  const [terminalMode, setTerminalMode] = useState<'logs' | 'pty'>('logs');
  const [logs, setLogs] = useState([
    { time: "00:00:00", agent: "System", msg: "Connecting to backend...", color: "text-zinc-500" }
  ]);
  const [pipeline, setPipeline] = useState<any[]>([]);
  const [activity, setActivity] = useState<any[]>([]);
  const [agentStatus, setAgentStatus] = useState<any>({
    Architect: 'idle',
    Visionary: 'idle',
    Reviewer: 'idle',
    Implementer: 'idle',
    Maintainer: 'idle',
  });

  const handleStartStop = async () => {
    if (!isRunning) {
      setIsRunning(true);
      setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: `Triggering AI Agent Loop for ${repoUrl}...`, color: "text-zinc-500" }]);
      try {
        const backendUrl = getBackendUrl();
        await fetch(`${backendUrl}/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
            repo_name: repoUrl,
            target_issue: targetIssue ? parseInt(targetIssue.replace('#',''), 10) : null 
          })
        });
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
        await fetch(`${backendUrl}/stop`, { method: "POST" });
      } catch (err) {
        console.error("Failed to stop agents:", err);
      }
    }
  };

  useEffect(() => {
    const backendUrl = getBackendUrl();
    const wsProtocol = backendUrl.startsWith("https") ? "wss:" : "ws:";
    const wsHost = backendUrl.replace(/^https?:\/\//, "");
    const ws = new WebSocket(`${wsProtocol}//${wsHost}/ws`);
    
    ws.onopen = () => {
      setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: "Connected to AutoMaintainer Core", color: "text-emerald-500" }]);
    };

    ws.onerror = (error) => {
      console.error("WebSocket error:", error);
      setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), agent: "System", msg: "WebSocket connection error. Check backend connectivity.", color: "text-red-400" }]);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'ui_update') {
          if (data.agentStatus) {
             setAgentStatus((prev: any) => ({ ...prev, ...data.agentStatus }));
          }
          if (data.pipeline) {
             setPipeline((prev) => {
               // Update existing or add new
               const exists = prev.find(p => p.id === data.pipeline.id);
               if (exists) return prev.map(p => p.id === data.pipeline.id ? data.pipeline : p);
               return [data.pipeline, ...prev];
             });
          }
          if (data.activity) {
             setActivity((prev) => [data.activity, ...prev]);
          }
        } else {
          setLogs(prev => [...prev, {
            time: new Date().toLocaleTimeString(),
            agent: data.agent || "System",
            msg: data.msg || JSON.stringify(data),
            color: data.color || "text-zinc-400"
          }]);
        }
      } catch (e) {
        console.error("Failed to parse WS message", e);
      }
    };
    
    return () => ws.close();
  }, []);

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
                <div className="flex items-center justify-between cursor-pointer" onClick={() => setIsEditingRepo(true)}>
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

              <LinkRow icon={<Settings className="w-4 h-4 text-zinc-400" />} label="Settings" />
            </div>
          </div>
        </div>
        
        {/* System Status Footer */}
        <div className="p-4 border-t border-zinc-800/50">
          <button 
            onClick={handleStartStop}
            className={`w-full py-2.5 px-4 rounded-lg flex items-center justify-center gap-2 text-sm font-medium transition-all ${
              isRunning 
                ? 'bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20' 
                : 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 border border-emerald-500/20'
            }`}
          >
            {isRunning ? <Square className="w-4 h-4 fill-current" /> : <Play className="w-4 h-4 fill-current" />}
            {isRunning ? 'Stop Loop' : 'Start Agents'}
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-screen overflow-hidden relative bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-zinc-900/40 via-[#0a0a0a] to-[#0a0a0a]">
        
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
          </div>
        </header>

          {/* Dashboard Grid, GitNexus, or IDE */}
          {activeTab === 'ide' ? (
            <main className="flex-1 flex flex-col overflow-hidden">
              <div className="flex-1 min-h-0">
                <WebIDE repoUrl={repoUrl} />
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
                    <div className="p-3 font-mono text-[11px] text-zinc-400 overflow-y-auto custom-scrollbar space-y-1.5 flex-1 flex flex-col">
                      {logs.map((log, i) => (
                        <LogLine key={i} time={log.time} agent={log.agent} msg={log.msg} color={log.color} />
                      ))}
                      {isRunning && (
                        <motion.div 
                          initial={{ opacity: 0 }} 
                          animate={{ opacity: 1 }} 
                          transition={{ repeat: Infinity, duration: 1.5, ease: "easeInOut" }}
                          className="text-zinc-500 mt-auto"
                        >
                          _
                        </motion.div>
                      )}
                    </div>
                  ) : (
                    <div className="flex-1 flex flex-col min-h-0 bg-[#1e1e1e]">
                      <InteractiveTerminal repoUrl={repoUrl} />
                    </div>
                  )}
              </div>
            </main>
          ) : activeTab === 'gitnexus' ? (
            <main className="flex-1 p-8 flex flex-col items-center justify-center text-center">
              <div className="max-w-md space-y-6">
                <div className="w-16 h-16 bg-indigo-500/10 rounded-2xl flex items-center justify-center mx-auto mb-6 shadow-[0_0_30px_-5px_rgba(99,102,241,0.2)]">
                  <FileCode className="w-8 h-8 text-indigo-400" />
                </div>
                <h2 className="text-xl font-bold text-white tracking-tight">Code Graph Active in Backend</h2>
                <p className="text-zinc-400 text-sm leading-relaxed">
                  The GitNexus MCP server is running successfully in the cloud on Port 4747. The AutoMaintainer AI agents are using it to intelligently navigate your repository!
                </p>
                <div className="bg-[#0d0d0d] border border-zinc-800/80 rounded-xl p-5 mt-6 text-left shadow-lg">
                  <p className="text-xs text-zinc-500 mb-2 font-medium uppercase tracking-wider">Want to view the graph?</p>
                  <p className="text-xs text-zinc-400 mb-4 leading-relaxed">Because GitNexus is a Zero-Server tool, its Web UI strictly connects to your local machine for privacy. To view the graph locally, run this in your terminal:</p>
                  <div className="bg-black/50 p-3 rounded-lg border border-zinc-800 flex items-center gap-3">
                    <Terminal className="w-4 h-4 text-zinc-500 shrink-0" />
                    <code className="text-xs text-emerald-400 font-mono">npx gitnexus@latest serve</code>
                  </div>
                </div>
              </div>
            </main>
          ) : (
        <main className="flex-1 overflow-y-auto p-8">
          <div className="max-w-6xl mx-auto space-y-6">
            
            <div className="grid grid-cols-3 gap-6">
              {/* Feature Pipeline */}
              <div className="col-span-2 space-y-6">
                <DashboardCard title="Active Feature Pipeline">
                  <div className="space-y-4">
                    {pipeline.length === 0 && (
                      <div className="text-sm text-zinc-500 text-center py-8">Waiting for AI agents to start a pipeline...</div>
                    )}
                    {pipeline.map((p, i) => (
                      <PipelineItem key={i} {...p} />
                    ))}
                  </div>
                </DashboardCard>
                
                {/* Live Terminal Log */}
                <div className="rounded-xl border border-zinc-800/50 bg-[#0d0d0d] shadow-2xl overflow-hidden flex flex-col h-80">
                  <div className="h-10 bg-zinc-900/80 border-b border-zinc-800/50 flex items-center px-4 gap-2">
                    <Terminal className="w-4 h-4 text-zinc-500" />
                    <span className="text-xs font-mono text-zinc-400">agent_execution_log.sh</span>
                  </div>
                  <div className="p-4 font-mono text-xs text-zinc-400 overflow-y-auto space-y-2 flex-1 flex flex-col">
                    {logs.map((log, i) => (
                      <LogLine key={i} time={log.time} agent={log.agent} msg={log.msg} color={log.color} />
                    ))}
                    {isRunning && (
                      <motion.div 
                        initial={{ opacity: 0 }} 
                        animate={{ opacity: 1 }} 
                        transition={{ repeat: Infinity, duration: 1.5, ease: "easeInOut" }}
                        className="text-zinc-500 mt-auto"
                      >
                        _
                      </motion.div>
                    )}
                  </div>
                </div>
              </div>

              {/* Side Stats */}
              <div className="space-y-6">
                <DashboardCard title="Recent Activity">
                  <div className="space-y-4 relative before:absolute before:inset-0 before:ml-2.5 before:-translate-x-px md:before:mx-auto md:before:translate-x-0 before:h-full before:w-0.5 before:bg-gradient-to-b before:from-transparent before:via-zinc-800/80 before:to-transparent">
                     {activity.length === 0 && (
                       <div className="text-sm text-zinc-500 text-center py-4 relative z-10 bg-[#0d0d0d]">No recent activity</div>
                     )}
                     {activity.map((a, i) => (
                       <ActivityItem key={i} {...a} />
                     ))}
                  </div>
                </DashboardCard>
                
                <div className="rounded-xl p-5 border border-indigo-500/20 bg-indigo-500/5 shadow-[0_0_30px_-5px_rgba(99,102,241,0.1)]">
                   <h3 className="text-sm font-semibold text-indigo-400 mb-2 flex items-center gap-2">
                     <Activity className="w-4 h-4" /> System Health
                   </h3>
                   <div className="space-y-3 mt-4">
                     <div className="flex justify-between items-center text-xs">
                       <span className="text-zinc-400">API Latency</span>
                       <span className="text-zinc-200">245ms</span>
                     </div>
                     <div className="w-full bg-zinc-800/50 rounded-full h-1.5">
                       <div className="bg-indigo-400 h-1.5 rounded-full w-1/3"></div>
                     </div>
                     <div className="flex justify-between items-center text-xs pt-2">
                       <span className="text-zinc-400">Token Usage (24h)</span>
                       <span className="text-zinc-200">142k / 500k</span>
                     </div>
                     <div className="w-full bg-zinc-800/50 rounded-full h-1.5">
                       <div className="bg-purple-400 h-1.5 rounded-full w-[28%]"></div>
                     </div>
                   </div>
                </div>
              </div>
            </div>

          </div>
        </main>
        )}
      </div>
    </div>
  );
}

function AgentRow({ name, role, icon, status }: any) {
  return (
    <div className="flex items-center gap-3 p-2 rounded-lg hover:bg-zinc-800/50 transition-colors cursor-default group">
      <div className="w-8 h-8 rounded-md bg-zinc-900 border border-zinc-800/80 flex items-center justify-center relative shadow-sm">
        {icon}
        {status === 'active' && (
          <span className="absolute -top-1 -right-1 flex h-2.5 w-2.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500 border-2 border-[#0d0d0d]"></span>
          </span>
        )}
      </div>
      <div className="flex-1">
        <h3 className="text-sm font-medium text-zinc-200 group-hover:text-white transition-colors">{name}</h3>
        <p className="text-[10px] text-zinc-500">{role}</p>
      </div>
    </div>
  );
}

function LinkRow({ icon, label, value }: any) {
  return (
    <div className="flex items-center justify-between p-2 rounded-lg hover:bg-zinc-800/50 transition-colors cursor-pointer text-sm">
      <div className="flex items-center gap-3 text-zinc-400">
        {icon}
        <span>{label}</span>
      </div>
      {value && <span className="text-xs font-mono text-zinc-500 truncate max-w-[100px]">{value}</span>}
    </div>
  );
}

function DashboardCard({ title, children }: any) {
  return (
    <div className="rounded-xl border border-zinc-800/60 bg-[#0d0d0d]/80 backdrop-blur-xl p-6 shadow-xl">
      <h2 className="text-sm font-semibold tracking-wide text-zinc-100 mb-6">{title}</h2>
      {children}
    </div>
  );
}

function PipelineItem({ id, title, status, agent }: any) {
  const statusColors: any = {
    'brainstorming': 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    'reviewing': 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    'implementing': 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  };

  return (
    <div className="flex items-center justify-between p-4 rounded-lg border border-zinc-800/40 bg-zinc-900/30 hover:bg-zinc-800/40 transition-colors">
      <div className="flex items-center gap-4">
        <span className="text-xs font-mono text-zinc-500">{id}</span>
        <span className="text-sm font-medium text-zinc-200">{title}</span>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500 hidden sm:block">{agent}</span>
        <span className={`text-[10px] px-2.5 py-1 rounded-full border uppercase font-medium tracking-wide ${statusColors[status]}`}>
          {status}
        </span>
      </div>
    </div>
  );
}

function LogLine({ time, agent, msg, color }: any) {
  return (
    <div className="flex gap-3 hover:bg-zinc-800/30 px-2 py-0.5 rounded transition-colors">
      <span className="text-zinc-600 shrink-0">[{time}]</span>
      <span className={`shrink-0 font-medium ${color}`}>[{agent}]</span>
      <span className="text-zinc-300">{msg}</span>
    </div>
  );
}

function ActivityItem({ title, time, type }: any) {
  return (
    <div className="relative flex items-center justify-between md:justify-normal md:odd:flex-row-reverse group is-active">
        <div className="flex items-center justify-center w-6 h-6 rounded-full border border-zinc-800 bg-zinc-900 shrink-0 md:order-1 md:group-odd:-translate-x-1/2 md:group-even:translate-x-1/2 shadow-sm relative z-10 text-zinc-500">
           {type === 'merge' ? <GitPullRequest className="w-3 h-3 text-purple-400" /> : <Search className="w-3 h-3 text-emerald-400" />}
        </div>
        <div className="w-[calc(100%-2.5rem)] md:w-[calc(50%-1.5rem)] p-3 rounded-lg border border-zinc-800/50 bg-zinc-900/30">
            <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-zinc-300">{title}</span>
                <span className="text-[10px] text-zinc-500 font-mono">{time}</span>
            </div>
        </div>
    </div>
  );
}
