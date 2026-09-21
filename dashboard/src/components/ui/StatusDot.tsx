interface StatusDotProps {
  status: "active" | "idle" | "error" | string;
  pulse?: boolean;
  size?: "sm" | "md";
}

const STATUS_COLORS: Record<string, string> = {
  active: "bg-emerald-500",
  idle: "bg-zinc-500",
  error: "bg-red-500",
  running: "bg-emerald-500",
  completed: "bg-blue-500",
  failed: "bg-red-500",
  cancelled: "bg-amber-500",
};

export function StatusDot({
  status,
  pulse = false,
  size = "sm",
}: StatusDotProps) {
  const color = STATUS_COLORS[status] || "bg-zinc-500";
  const sizeClass = size === "md" ? "h-2.5 w-2.5" : "h-2 w-2";
  const shouldPulse = pulse || status === "active" || status === "running";

  return (
    <span className={`relative flex ${sizeClass}`}>
      {shouldPulse && (
        <span
          className={`absolute inline-flex h-full w-full rounded-full ${color} opacity-75 animate-pulse`}
        />
      )}
      <span
        className={`relative inline-flex rounded-full ${sizeClass} ${color}`}
      />
    </span>
  );
}
