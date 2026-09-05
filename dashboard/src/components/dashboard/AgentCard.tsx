import { StatusDot } from "@/components/ui/StatusDot";

interface AgentCardProps {
  name: string;
  role: string;
  icon: React.ReactNode;
  status: string;
}

export function AgentCard({ name, role, icon, status }: AgentCardProps) {
  const isActive = status === "active";

  return (
    <div
      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg border transition-colors ${
        isActive
          ? "border-indigo-500/30 bg-indigo-500/5"
          : "border-zinc-800/60 bg-zinc-900/40"
      }`}
    >
      <div className="w-8 h-8 rounded-lg bg-zinc-800/60 flex items-center justify-center shrink-0">
        {icon}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-zinc-200 truncate">{name}</p>
        <p className="text-[11px] text-zinc-500 truncate">{role}</p>
      </div>
      <div className="flex items-center gap-1.5">
        <StatusDot status={status} size="sm" />
        <span className="text-[11px] text-zinc-500 capitalize">{status}</span>
      </div>
    </div>
  );
}
