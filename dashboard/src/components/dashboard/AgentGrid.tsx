import {
  BrainCircuit,
  Search,
  CheckCircle,
  FileCode,
  GitPullRequest,
} from "lucide-react";
import { AgentCard } from "./AgentCard";

interface AgentGridProps {
  agentStatus: Record<string, string>;
}

const AGENTS = [
  {
    name: "Architect",
    role: "Principal Engineer",
    icon: <BrainCircuit className="w-4 h-4 text-rose-400" />,
  },
  {
    name: "Visionary",
    role: "Brainstormer",
    icon: <Search className="w-4 h-4 text-emerald-400" />,
  },
  {
    name: "Reviewer",
    role: "Product Manager",
    icon: <CheckCircle className="w-4 h-4 text-amber-400" />,
  },
  {
    name: "Implementer",
    role: "Developer",
    icon: <FileCode className="w-4 h-4 text-blue-400" />,
  },
  {
    name: "Maintainer",
    role: "Senior Engineer",
    icon: <GitPullRequest className="w-4 h-4 text-purple-400" />,
  },
];

export function AgentGrid({ agentStatus }: AgentGridProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
      {AGENTS.map((agent) => (
        <AgentCard
          key={agent.name}
          name={agent.name}
          role={agent.role}
          icon={agent.icon}
          status={agentStatus[agent.name] || "idle"}
        />
      ))}
    </div>
  );
}
