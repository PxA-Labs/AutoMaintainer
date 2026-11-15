'use client';

import React, { createContext, useContext, useEffect, useState, ReactNode } from 'react';
import { User, Session, AuthError } from '@supabase/supabase-js';
import { getBrowserClient, getSupabaseConfigError } from '@/lib/supabase';

interface AuthContextType {
  user: User | null;
  session: Session | null;
  loading: boolean;
  signInWithGitHub: () => Promise<{ error: AuthError | null }>;
  signOut: () => Promise<void>;
  refreshSession: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

/**
 * Path Supabase redirects back to after GitHub OAuth (see app/auth/callback).
 * Trailing slash matches the exported auth/callback/index.html, avoiding a
 * redirect hop. Add it to the Supabase project's allowed redirect URLs.
 */
export const AUTH_CALLBACK_PATH = '/auth/callback/';

/**
 * Shown instead of the app when the build has no Supabase configuration.
 * NEXT_PUBLIC_* values are inlined at build time, so this is a deployment
 * problem the user cannot fix from the browser; say so rather than crash.
 */
export function SupabaseConfigError({ message }: { message: string }) {
  return (
    <div role="alert" className="flex min-h-dvh w-full bg-zinc-950 items-center justify-center p-8">
      <div className="max-w-md text-center">
        <h1 className="text-xl font-semibold text-zinc-100 mb-2">Dashboard is not configured</h1>
        <p className="text-sm text-zinc-400 mb-4">{message}</p>
        <p className="text-xs text-zinc-500">
          These variables are read when the dashboard is built. Rebuild it with them set.
        </p>
      </div>
    </div>
  );
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const configError = getSupabaseConfigError();
  const [user, setUser] = useState<User | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (configError) return;
    const supabase = getBrowserClient();

    // Get initial session
    supabase.auth
      .getSession()
      .then(({ data: { session } }) => {
        setSession(session);
        setUser(session?.user ?? null);
      })
      .catch((err) => {
        console.error('Failed to read the Supabase session:', err);
      })
      .finally(() => setLoading(false));

    // Listen for auth changes
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        setSession(session);
        setUser(session?.user ?? null);
        setLoading(false);
      }
    );

    return () => subscription.unsubscribe();
  }, [configError]);

  if (configError) {
    return <SupabaseConfigError message={configError} />;
  }

  const signInWithGitHub = async () => {
    const supabase = getBrowserClient();
    const { error } = await supabase.auth.signInWithOAuth({
      provider: 'github',
      options: {
        redirectTo: `${window.location.origin}${AUTH_CALLBACK_PATH}`,
        scopes: 'repo user:email read:org',
      },
    });
    return { error };
  };

  const signOut = async () => {
    const supabase = getBrowserClient();
    await supabase.auth.signOut();
  };

  const refreshSession = async () => {
    const supabase = getBrowserClient();
    const { data: { session } } = await supabase.auth.refreshSession();
    setSession(session);
    setUser(session?.user ?? null);
  };

  return (
    <AuthContext.Provider value={{ user, session, loading, signInWithGitHub, signOut, refreshSession }}>
      {children}
    </AuthContext.Provider>
  );
}

const defaultAuthContext: AuthContextType = {
  user: null,
  session: null,
  loading: false,
  signInWithGitHub: async () => ({ error: null }),
  signOut: async () => {},
  refreshSession: async () => {},
};

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    return defaultAuthContext;
  }
  return context;
}
