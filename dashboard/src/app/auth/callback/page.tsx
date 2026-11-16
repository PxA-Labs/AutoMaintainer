"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { AlertCircle, Loader2 } from "lucide-react";
import { getBrowserClient } from "@/lib/supabase";
import { readOAuthError } from "@/lib/oauth";
import { consumeReturnTo } from "@/lib/auth-return";

/**
 * OAuth landing page (redirectTo in AuthProvider.signInWithGitHub).
 *
 * This is a client page so it works with the static export. The Supabase
 * client is created with detectSessionInUrl, so it exchanges the `?code=`
 * (PKCE) or reads the `#access_token` (implicit) on initialisation;
 * getSession() resolves once that has finished. We then leave for the
 * dashboard, or show the provider's error.
 */

export default function AuthCallbackPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const urlError = readOAuthError(window.location.href);
    const supabase = getBrowserClient();

    // Back to where sign-in was started (AuthProvider.signInWithGitHub). Read
    // once: finish() can run twice (auth event and getSession) and the
    // stored value is forgotten on the first read.
    let returnTo: string | null = null;
    const finish = () => {
      if (!active) return;
      returnTo ??= consumeReturnTo();
      router.replace(returnTo);
    };

    // Covers the case where the exchange completes after getSession resolved.
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (session && (event === "SIGNED_IN" || event === "INITIAL_SESSION")) finish();
    });

    supabase.auth
      .getSession()
      .then(({ data, error: sessionError }) => {
        if (!active) return;
        if (data.session) {
          finish();
          return;
        }
        setError(
          urlError ??
            sessionError?.message ??
            "Sign-in did not complete. The login link may have expired; please try again."
        );
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, [router]);

  return (
    <div className="flex min-h-dvh w-full bg-zinc-950 items-center justify-center p-8">
      {error ? (
        <div role="alert" className="max-w-md text-center">
          <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-3" aria-hidden="true" />
          <h1 className="text-lg font-semibold text-zinc-100 mb-2">Sign-in failed</h1>
          <p className="text-sm text-zinc-400 mb-6">{error}</p>
          <Link
            href="/"
            className="inline-flex px-4 py-2 rounded-lg bg-zinc-100 text-zinc-900 text-sm font-medium hover:bg-white"
          >
            Back to dashboard
          </Link>
        </div>
      ) : (
        <div className="flex items-center gap-3 text-sm text-zinc-400" aria-live="polite">
          <Loader2 className="w-5 h-5 text-indigo-400 animate-spin" aria-hidden="true" />
          Completing sign-in...
        </div>
      )}
    </div>
  );
}
