import { BrainCircuit, LogIn } from "lucide-react";

interface AuthGateProps {
  onSignIn: () => void;
  onSkip: () => void;
  loading: boolean;
}

export function AuthGate({ onSignIn, onSkip, loading }: AuthGateProps) {
  return (
    <div className="flex min-h-dvh w-full bg-background items-center justify-center">
      <div className="text-center p-8 max-w-md">
        <div className="w-14 h-14 rounded-xl bg-zinc-800 flex items-center justify-center mx-auto mb-6">
          <BrainCircuit className="w-7 h-7 text-zinc-300" />
        </div>
        <h1 className="text-2xl font-semibold text-zinc-100 tracking-tight mb-2">
          AutoMaintainer
        </h1>
        <p className="text-sm text-zinc-500 mb-8 leading-relaxed max-w-sm mx-auto">
          An always-on autonomous AI engineering team.
          Sign in with GitHub to start managing your repositories.
        </p>
        <div className="flex flex-col items-center gap-3">
          <button
            onClick={onSignIn}
            disabled={loading}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-zinc-100 text-zinc-900 rounded-lg font-medium text-sm hover:bg-white transition-colors active:scale-[0.98]"
          >
            <LogIn className="w-4 h-4" />
            Sign in with GitHub
          </button>
          <button
            onClick={onSkip}
            className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors py-1"
          >
            Skip for now
          </button>
        </div>
      </div>
    </div>
  );
}
