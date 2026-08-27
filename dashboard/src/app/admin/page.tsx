/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useEffect, useCallback } from "react";
import { 
  Users, Database, Activity, DollarSign, TrendingUp, AlertCircle, 
  Clock, RefreshCw, Loader2,
  BarChart3, Server, GitBranch, Terminal, Settings
} from "lucide-react";
import { motion } from "framer-motion";
import { useAuth, AuthProvider } from '@/lib/auth';

interface OrganizationMetric {
  org_id: string;
  org_name: string;
  slug: string;
  plan: string;
  member_count: number;
  repo_count: number;
  active_runs: number;
  runs_24h: number;
  failed_24h: number;
  estimated_cost_30d_usd: number;
  last_run_at: string | null;
}

interface AdminMetrics {
  orgs: {
    total: number;
    active: number;
    byPlan: Record<string, number>;
  };
  organizations: OrganizationMetric[];
  runs: {
    total: number;
    running: number;
    completed: number;
    failed: number;
    cancelled?: number;
    last24h: number;
  };
  usage: {
    totalTokens: number;
    totalCostCents: number;
    byEventType: Record<string, { count: number; costCents: number }>;
    last30Days: Array<{ date: string; tokens: number; costCents: number }>;
  };
  repos: {
    total: number;
    private: number | null;
    public: number | null;
  };
  system: {
    supabaseHealthy: boolean | null;
    activeConnections: number | null;
    queueDepth: number | null;
  };
}

const formatNumber = (num: number) => {
  if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
  if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
  return num.toString();
};

const formatCost = (cents: number) => {
  return '$' + (cents / 100).toFixed(2);
};

const statusBadge = (status: string) => {
  const styles: Record<string, string> = {
    running: 'bg-emerald-500/20 text-emerald-400',
    completed: 'bg-blue-500/20 text-blue-400',
    failed: 'bg-red-500/20 text-red-400',
    cancelled: 'bg-amber-500/20 text-amber-400',
    queued: 'bg-zinc-500/20 text-zinc-400',
  };
  return <span className={`px-2 py-0.5 rounded text-xs font-medium ${styles[status] || styles.queued}`}>{status}</span>;
};

function AdminDashboard() {
  const { user, session, loading: authLoading } = useAuth();
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<'overview' | 'orgs' | 'runs' | 'usage' | 'system'>('overview');

  const fetchMetrics = useCallback(async () => {
    if (!session) return;
    
    try {
      setLoading(true);
      setError(null);
      
      const backendUrl = (() => {
        if (process.env.NEXT_PUBLIC_BACKEND_URL) {
          return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
        }
        if (typeof window !== "undefined") {
          const port = window.location.port;
          const hostname = window.location.hostname;
          if ((hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") && port !== "8000") {
            const formattedHost = (hostname === "::1" || hostname === "[::1]") ? "[::1]" : hostname;
            return `${window.location.protocol}//${formattedHost}:8000`;
          }
          return `${window.location.protocol}//${window.location.host}`;
        }
        return "http://localhost:8000";
      })();

      const res = await fetch(`${backendUrl}/admin/metrics`, {
        headers: { "Authorization": `Bearer ${session.access_token}` }
      });
      
      if (!res.ok) {
        if (res.status === 403) {
          throw new Error("Access denied. Admin privileges required.");
        }
        throw new Error(`Failed to fetch metrics: ${res.status}`);
      }
      
      const data = await res.json();
      setMetrics(data);
    } catch (err) {
      console.error("Failed to fetch admin metrics:", err);
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [session]);

  useEffect(() => {
    const loadMetrics = async () => {
      await fetchMetrics();
    };
    loadMetrics();
  }, [fetchMetrics]);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      if (!refreshing) {
        setRefreshing(true);
        fetchMetrics();
      }
    }, 30000);
    
    return () => clearInterval(interval);
  }, [fetchMetrics, refreshing]);

  // Auth gate
  if (authLoading) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <div className="text-center p-8">
          <AlertCircle className="w-16 h-16 text-red-500 mx-auto mb-4" />
          <h1 className="text-2xl font-bold text-zinc-100 mb-2">Admin Access Required</h1>
          <p className="text-zinc-500">Please sign in with an admin account.</p>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center p-8">
        <div className="text-center">
          <AlertCircle className="w-16 h-16 text-red-500 mx-auto mb-4" />
          <h1 className="text-2xl font-bold text-zinc-100 mb-2">Failed to Load Metrics</h1>
          <p className="text-zinc-500 mb-4">{error}</p>
          <button 
            onClick={() => { setRefreshing(true); fetchMetrics(); }}
            className="px-4 py-2 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-lg hover:bg-indigo-500/20 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!metrics) {
    return (
      <div className="flex h-screen w-full bg-[#0a0a0a] items-center justify-center">
        <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex h-screen w-full bg-[#0a0a0a] text-zinc-100 font-sans overflow-hidden">
      {/* Sidebar */}
      <div className="w-64 border-r border-zinc-800/50 bg-[#0d0d0d] flex flex-col">
        <div className="p-6 border-b border-zinc-800/50 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-purple-500/20">
            <Settings className="w-5 h-5 text-white" />
          </div>
          <h1 className="font-bold text-lg tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-zinc-100 to-zinc-400">
            Admin Dashboard
          </h1>
        </div>
        
        <nav className="flex-1 py-6 px-4 space-y-1 overflow-y-auto">
          {[
            { id: 'overview', label: 'Overview', icon: BarChart3 },
            { id: 'orgs', label: 'Organizations', icon: Users },
            { id: 'runs', label: 'Agent Runs', icon: Activity },
            { id: 'usage', label: 'Usage & Billing', icon: DollarSign },
            { id: 'system', label: 'System Health', icon: Server },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as any)}
              className={`w-full flex items-center gap-3 p-3 rounded-lg transition-all text-sm ${
                activeTab === tab.id 
                  ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20' 
                  : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'
              }`}
            >
              <tab.icon className="w-5 h-5" />
              {tab.label}
            </button>
          ))}
        </nav>
        
        <div className="p-4 border-t border-zinc-800/50">
          <button
            onClick={() => { setRefreshing(true); fetchMetrics(); }}
            disabled={refreshing}
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-lg font-medium hover:bg-indigo-500/20 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Main Content */}
      <main className="flex-1 flex flex-col h-screen overflow-hidden p-6">
        {/* Header */}
        <header className="mb-6">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-2xl font-bold">{activeTab.charAt(0).toUpperCase() + activeTab.slice(1)}</h2>
              <p className="text-zinc-500 text-sm">System administration and monitoring</p>
            </div>
            <div className="flex items-center gap-4 text-sm text-zinc-500">
              <span className="flex items-center gap-1">
                <span className={`w-2 h-2 rounded-full ${metrics.system.supabaseHealthy ? 'bg-emerald-400' : 'bg-red-400'}`} />
                Supabase: {metrics.system.supabaseHealthy === null ? 'Unknown' : metrics.system.supabaseHealthy ? 'Healthy' : 'Unhealthy'}
              </span>
              <span className="flex items-center gap-1">
                <Clock className="w-3.5 h-3.5" />
                Queue: {metrics.system.queueDepth === null ? 'Unknown' : metrics.system.queueDepth}
              </span>
            </div>
          </div>
        </header>

        {error && (
          <motion.div
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            className="mb-6 p-4 bg-red-500/10 border border-red-500/20 text-red-400 rounded-lg flex items-center gap-3"
          >
            <AlertCircle className="w-5 h-5 shrink-0" />
            <span>{error}</span>
          </motion.div>
        )}

        {/* Tab Content */}
        {activeTab === 'overview' && (
          <div className="flex-1 overflow-y-auto space-y-6">
            {/* Key Metrics Grid */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <MetricCard 
                title="Total Organizations" 
                value={metrics.orgs.total} 
                icon={Users} 
                color="indigo"
                subtitle={`${metrics.orgs.active} active`}
              />
              <MetricCard 
                title="Agent Runs (24h)" 
                value={metrics.runs.last24h} 
                icon={Activity} 
                color="emerald"
                subtitle={`${metrics.runs.running} running`}
              />
              <MetricCard 
                title="Total Repositories" 
                value={metrics.repos.total} 
                icon={GitBranch} 
                color="blue"
                subtitle={`${metrics.repos.private === null ? 'Visibility data unavailable' : `${metrics.repos.private} private`}`}
              />
              <MetricCard 
                title="Est. Cost (30d)" 
                value={formatCost(metrics.usage.totalCostCents)} 
                icon={DollarSign} 
                color="amber"
                subtitle={`${formatNumber(metrics.usage.totalTokens)} tokens`}
              />
            </div>

            {/* Charts Row */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <ChartCard title="Runs by Status" icon={Activity}>
                <div className="space-y-3">
                  {[
                    { label: 'Completed', value: metrics.runs.completed, color: 'bg-blue-500' },
                    { label: 'Running', value: metrics.runs.running, color: 'bg-emerald-500' },
                    { label: 'Failed', value: metrics.runs.failed, color: 'bg-red-500' },
                    { label: 'Cancelled', value: metrics.runs.cancelled || 0, color: 'bg-amber-500' },
                  ].map((item) => (
                    <div key={item.label} className="flex items-center gap-3">
                      <span className="w-24 text-xs text-zinc-400">{item.label}</span>
                      <div className="flex-1 h-2 bg-zinc-800 rounded-full overflow-hidden">
                        <div 
                          className={`h-full ${item.color} transition-all`}
                          style={{ width: `${metrics.runs.total > 0 ? item.value / metrics.runs.total * 100 : 0}%` }}
                        />
                      </div>
                      <span className="w-16 text-right text-xs font-mono text-zinc-200">{item.value}</span>
                    </div>
                  ))}
                </div>
              </ChartCard>

              <ChartCard title="Orgs by Plan" icon={Users}>
                <div className="space-y-3">
                  {Object.entries(metrics.orgs.byPlan).map(([plan, count]) => (
                    <div key={plan} className="flex items-center gap-3">
                      <span className="w-24 text-xs text-zinc-400 capitalize">{plan}</span>
                      <div className="flex-1 h-2 bg-zinc-800 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-indigo-500 transition-all"
                          style={{ width: `${metrics.orgs.total > 0 ? count / metrics.orgs.total * 100 : 0}%` }}
                        />
                      </div>
                      <span className="w-16 text-right text-xs font-mono text-zinc-200">{count}</span>
                    </div>
                  ))}
                </div>
              </ChartCard>
            </div>

            {/* Usage Trend */}
            <ChartCard title="Usage Trend (30 Days)" icon={TrendingUp} className="lg:col-span-2">
              <div className="h-64 flex items-end justify-around gap-1 px-2">
                {metrics.usage.last30Days.map((day, i) => (
                  <div key={i} className="flex-1 flex flex-col items-center gap-1">
                    <div 
                      className="w-full bg-blue-500/50 rounded-t transition-all hover:bg-blue-500"
                      style={{ height: `${Math.max(day.tokens / 10000 * 100, 2)}%`, minHeight: '4px' }}
                      title={`${day.date}: ${formatNumber(day.tokens)} tokens`}
                    />
                    <span className="text-[10px] text-zinc-500">{day.date.split('-').slice(1).join('-')}</span>
                  </div>
                ))}
              </div>
            </ChartCard>
          </div>
        )}

        {activeTab === 'orgs' && (
          <OrgTable metrics={metrics} />
        )}

        {activeTab === 'runs' && (
          <RunsTable />
        )}

        {activeTab === 'usage' && (
          <UsageTable metrics={metrics} />
        )}

        {activeTab === 'system' && (
          <SystemHealth metrics={metrics} />
        )}
      </main>
    </div>
  );
}

function MetricCard({ title, value, icon: Icon, color, subtitle }: { 
  title: string; 
  value: number | string; 
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  subtitle?: string;
}) {
  const colors: Record<string, string> = {
    indigo: 'bg-indigo-500/20 text-indigo-400 border-indigo-500/20',
    emerald: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/20',
    blue: 'bg-blue-500/20 text-blue-400 border-blue-500/20',
    amber: 'bg-amber-500/20 text-amber-400 border-amber-500/20',
    red: 'bg-red-500/20 text-red-400 border-red-500/20',
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className={`p-5 bg-zinc-900/50 border rounded-xl ${colors[color]}`}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-zinc-500 uppercase tracking-wider mb-1">{title}</p>
          <p className="text-3xl font-bold font-mono text-zinc-100">{value}</p>
          {subtitle && <p className="text-xs text-zinc-500 mt-1">{subtitle}</p>}
        </div>
        <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${colors[color]}`}>
          <Icon className="w-6 h-6" />
        </div>
      </div>
    </motion.div>
  );
}

function ChartCard({ title, icon: Icon, children, className = '' }: { 
  title: string; 
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`bg-zinc-900/50 border border-zinc-800/50 rounded-xl p-5 ${className}`}>
      <div className="flex items-center gap-2 mb-4">
        <Icon className="w-5 h-5 text-indigo-400" />
        <h3 className="font-medium text-zinc-300">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function OrgTable({ metrics }: { metrics: AdminMetrics }) {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-zinc-800/50">
          <h3 className="font-medium text-zinc-300">Organizations ({metrics.orgs.total})</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800/50 bg-zinc-800/30">
                {['Organization', 'Plan', 'Members', 'Repos', 'Runs (24h)', 'Cost (30d)', 'Last Active'].map((col) => (
                  <th key={col} className="px-4 py-3 text-left text-xs font-medium text-zinc-500 uppercase tracking-wider">{col}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/50">
              {metrics.organizations.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-12 text-center text-zinc-500">
                    No organization metrics are available.
                  </td>
                </tr>
              ) : (
                metrics.organizations.map((organization) => (
                  <tr key={organization.org_id} className="hover:bg-zinc-800/30">
                    <td className="px-4 py-3">
                      <div className="font-mono text-zinc-200">{organization.org_name}</div>
                      <div className="text-xs text-zinc-500">{organization.slug}</div>
                    </td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-indigo-500/20 text-indigo-400">
                        {organization.plan}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-zinc-300">{organization.member_count}</td>
                    <td className="px-4 py-3 text-zinc-300">{organization.repo_count}</td>
                    <td className="px-4 py-3 text-zinc-300">{organization.runs_24h}</td>
                    <td className="px-4 py-3 font-mono text-zinc-300">
                      ${Number(organization.estimated_cost_30d_usd || 0).toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-xs text-zinc-500">
                      {organization.last_run_at
                        ? new Date(organization.last_run_at).toLocaleString()
                        : "No runs"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <div className="px-5 py-4 border-t border-zinc-800/50 text-xs text-zinc-500">
          Showing {metrics.organizations.length} organization{metrics.organizations.length === 1 ? "" : "s"} in your organization scope.
        </div>
      </div>
    </div>
  );
}

function RunsTable() {
  const [runs, setRuns] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const { session } = useAuth();

  useEffect(() => {
    const fetchRuns = async () => {
      if (!session) return;
      try {
        const backendUrl = (() => {
          if (process.env.NEXT_PUBLIC_BACKEND_URL) return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
          if (typeof window !== "undefined") {
            const port = window.location.port;
            const hostname = window.location.hostname;
            if ((hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") && port !== "8000") {
              const formattedHost = (hostname === "::1" || hostname === "[::1]") ? "[::1]" : hostname;
              return `${window.location.protocol}//${formattedHost}:8000`;
            }
            return `${window.location.protocol}//${window.location.host}`;
          }
          return "http://localhost:8000";
        })();

        const res = await fetch(`${backendUrl}/admin/runs`, {
          headers: { "Authorization": `Bearer ${session.access_token}` }
        });
        if (res.ok) {
          const data = await res.json();
          setRuns(data.runs || []);
        }
      } catch (err) {
        console.error("Failed to fetch runs:", err);
      } finally {
        setLoading(false);
      }
    };
    fetchRuns();
  }, [session]);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-8 h-8 text-indigo-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="bg-zinc-900/50 border border-zinc-800/50 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-zinc-800/50 flex items-center justify-between">
          <h3 className="font-medium text-zinc-300">Recent Agent Runs ({runs.length})</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800/50 bg-zinc-800/30">
                {['Run ID', 'Org', 'Repo', 'Mode', 'Status', 'Agent', 'Iteration', 'Created', 'Duration'].map((col) => (
                  <th key={col} className="px-4 py-3 text-left text-xs font-medium text-zinc-500 uppercase tracking-wider">{col}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/50">
              {runs.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center text-zinc-500">No runs found</td>
                </tr>
              ) : (
                runs.slice(0, 50).map((run) => (
                  <tr key={run.id} className="hover:bg-zinc-800/30">
                    <td className="px-4 py-3 font-mono text-xs text-zinc-400">{run.id.substring(0,8)}...</td>
                    <td className="px-4 py-3 text-zinc-300">{run.org_id?.substring(0,8)}...</td>
                    <td className="px-4 py-3 text-zinc-300">{run.repo_name}</td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-zinc-800 text-zinc-300">{run.mode}</span>
                    </td>
                    <td className="px-4 py-3">{statusBadge(run.status)}</td>
                    <td className="px-4 py-3 text-zinc-400">{run.current_agent || '-'}</td>
                    <td className="px-4 py-3 text-zinc-300">{run.iteration || 0}/{run.max_iterations || 3}</td>
                    <td className="px-4 py-3 text-xs text-zinc-500">{new Date(run.created_at).toLocaleString()}</td>
                    <td className="px-4 py-3 text-xs text-zinc-500">
                      {run.completed_at && run.started_at 
                        ? Math.round((new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000) + 's'
                        : run.status === 'running' ? 'In progress' : '-'
                      }
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function UsageTable({ metrics }: { metrics: AdminMetrics }) {
  return (
    <div className="flex-1 overflow-y-auto space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ChartCard title="Usage by Event Type" icon={Activity}>
          <div className="space-y-3">
            {Object.entries(metrics.usage.byEventType).map(([event, data]) => (
              <div key={event} className="flex items-center gap-3">
                <span className="w-36 text-xs text-zinc-400">{event.replace(/_/g, ' ')}</span>
                <div className="flex-1 h-2 bg-zinc-800 rounded-full overflow-hidden">
                  <div 
                    className="h-full bg-blue-500 transition-all"
                    style={{ width: `${metrics.usage.totalTokens > 0 ? data.count / metrics.usage.totalTokens * 100 : 0}%` }}
                  />
                </div>
                <span className="w-20 text-right text-xs font-mono text-zinc-200">{data.count}</span>
                <span className="w-20 text-right text-xs font-mono text-amber-400">{formatCost(data.costCents)}</span>
              </div>
            ))}
          </div>
        </ChartCard>

        <ChartCard title="Cost Breakdown (30d)" icon={DollarSign}>
          <div className="space-y-3">
            {Object.entries(metrics.usage.byEventType)
              .sort(([,a], [,b]) => b.costCents - a.costCents)
              .map(([event, data]) => (
                <div key={event} className="flex items-center justify-between py-2">
                  <span className="text-xs text-zinc-400">{event.replace(/_/g, ' ')}</span>
                  <div className="flex items-center gap-4">
                    <span className="text-xs font-mono text-zinc-300">{data.count}</span>
                    <span className="text-xs font-mono text-amber-400">{formatCost(data.costCents)}</span>
                  </div>
                </div>
              ))}
            <div className="pt-2 border-t border-zinc-800/50 flex items-center justify-between">
              <span className="text-xs font-medium text-zinc-300">Total</span>
              <span className="text-xs font-mono text-amber-400">{formatCost(metrics.usage.totalCostCents)}</span>
            </div>
          </div>
        </ChartCard>
      </div>

      <ChartCard title="Daily Usage (30 Days)" icon={TrendingUp} className="lg:col-span-2">
        <div className="h-80 flex items-end justify-around gap-1 px-2 relative">
          {metrics.usage.last30Days.map((day, i) => (
            <div key={i} className="flex-1 flex flex-col items-center gap-1 relative" style={{ maxWidth: '40px' }}>
              <div 
                className="w-full bg-blue-500/50 rounded-t transition-all hover:bg-blue-500 cursor-pointer"
                style={{ height: `${Math.max(day.tokens / 50000 * 100, 2)}%`, minHeight: '4px' }}
                title={`${day.date}: ${formatNumber(day.tokens)} tokens • ${formatCost(day.costCents)}`}
              />
              <span className="text-[10px] text-zinc-500">{day.date.split('-').slice(1).join('-')}</span>
            </div>
          ))}
        </div>
      </ChartCard>
    </div>
  );
}

function SystemHealth({ metrics }: { metrics: AdminMetrics }) {
  return (
    <div className="flex-1 overflow-y-auto space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <HealthCard 
          title="Supabase Database" 
          status={metrics.system.supabaseHealthy === null ? 'unknown' : metrics.system.supabaseHealthy ? 'healthy' : 'unhealthy'}
          icon={Database}
          checks={[
            { label: 'Connection', status: metrics.system.supabaseHealthy === null ? 'unknown' : metrics.system.supabaseHealthy ? 'ok' : 'fail' },
            { label: 'Realtime', status: 'unknown' },
            { label: 'RLS Policies', status: 'unknown' },
            { label: 'Migrations', status: 'unknown' },
          ]}
        />
        <HealthCard 
          title="Redis / Celery" 
          status="unknown"
          icon={Terminal}
          checks={[
            { label: 'Redis Connection', status: 'unknown' },
            { label: 'Worker Count', status: 'unknown' },
            { label: 'Queue Depth', status: metrics.system.queueDepth === null ? 'unknown' : metrics.system.queueDepth > 100 ? 'warn' : 'ok', detail: metrics.system.queueDepth === null ? 'Not reported' : `${metrics.system.queueDepth} jobs` },
            { label: 'Beat Scheduler', status: 'unknown' },
          ]}
        />
        <HealthCard 
          title="GitHub Integration" 
          status="unknown"
          icon={GitBranch}
          checks={[
            { label: 'App Installation', status: 'unknown' },
            { label: 'Webhook Delivery', status: 'unknown' },
            { label: 'Token Refresh', status: 'unknown' },
            { label: 'API Rate Limit', status: 'unknown' },
          ]}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ChartCard title="System Resources" icon={Server}>
          <div className="space-y-4">
            <ResourceBar label="CPU Usage" value={null} warning={80} critical={95} />
            <ResourceBar label="Memory Usage" value={null} warning={80} critical={95} />
            <ResourceBar label="Disk Usage" value={null} warning={80} critical={95} />
            <ResourceBar label="Network I/O" value={null} warning={80} critical={95} />
          </div>
        </ChartCard>

        <ChartCard title="Recent Alerts" icon={AlertCircle}>
          <div className="space-y-2">
            <p className="text-sm text-zinc-500">No live alert feed is configured for this dashboard.</p>
          </div>
        </ChartCard>
      </div>
    </div>
  );
}

function HealthCard({ title, status, icon: Icon, checks }: { 
  title: string; 
  status: 'healthy' | 'degraded' | 'unhealthy' | 'unknown';
  icon: React.ComponentType<{ className?: string }>;
  checks: Array<{ label: string; status: 'ok' | 'warn' | 'fail' | 'unknown'; detail?: string }>;
}) {
  const statusStyles = {
    healthy: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/20',
    degraded: 'bg-amber-500/20 text-amber-400 border-amber-500/20',
    unhealthy: 'bg-red-500/20 text-red-400 border-red-500/20',
    unknown: 'bg-zinc-500/20 text-zinc-400 border-zinc-500/20',
  };

  return (
    <div className={`p-5 bg-zinc-900/50 border rounded-xl ${statusStyles[status]}`}>
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg ${statusStyles[status]} flex items-center justify-center`}>
            <Icon className="w-5 h-5" />
          </div>
          <div>
            <h4 className="font-medium text-zinc-100">{title}</h4>
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${statusStyles[status]}`}>
              {status.charAt(0).toUpperCase() + status.slice(1)}
            </span>
          </div>
        </div>
      </div>
      <div className="space-y-2">
        {checks.map((check, i) => (
          <div key={i} className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">{check.label}</span>
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${
                check.status === 'ok' ? 'bg-emerald-400' :
                check.status === 'warn' ? 'bg-amber-400' :
                check.status === 'unknown' ? 'bg-zinc-500' :
                'bg-red-400'
              }`} />
              {check.detail && <span className="text-xs text-zinc-500">{check.detail}</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ResourceBar({ label, value, warning, critical }: { 
  label: string; 
  value: number | null;
  warning: number; 
  critical: number;
}) {
  const color = value === null ? 'bg-zinc-600' : value >= critical ? 'bg-red-500' : value >= warning ? 'bg-amber-500' : 'bg-emerald-500';
  
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-zinc-400">{label}</span>
        <span className="font-mono text-zinc-200">{value === null ? 'Unknown' : `${value}%`}</span>
      </div>
      <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
        <div 
          className={`h-full ${color} transition-all`}
          style={{ width: `${value ?? 0}%` }}
        />
      </div>
    </div>
  );
}

export default function AdminPage() {
  return (
    <AuthProvider>
      <AdminDashboard />
    </AuthProvider>
  );
}