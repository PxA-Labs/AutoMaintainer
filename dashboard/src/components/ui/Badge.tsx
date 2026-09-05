interface BadgeProps {
  children: React.ReactNode;
  variant?: "default" | "success" | "warning" | "error" | "info" | "accent";
}

const BADGE_STYLES: Record<string, string> = {
  default: "bg-zinc-800 text-zinc-400",
  success: "bg-emerald-500/15 text-emerald-400",
  warning: "bg-amber-500/15 text-amber-400",
  error: "bg-red-500/15 text-red-400",
  info: "bg-blue-500/15 text-blue-400",
  accent: "bg-indigo-500/15 text-indigo-400",
};

export function Badge({ children, variant = "default" }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${BADGE_STYLES[variant]}`}
    >
      {children}
    </span>
  );
}
