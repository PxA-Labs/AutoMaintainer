import { createClient } from '@supabase/supabase-js';

export function sanitizeSupabaseUrl(rawUrl?: string): string {
  if (!rawUrl) return 'https://placeholder.supabase.co';
  let cleaned = rawUrl.trim().replace(/^['"]+|['"]+$/g, '');
  if (!cleaned) return 'https://placeholder.supabase.co';

  // If user pasted just the reference id like "wcskhdvvlnplgynhwfqq"
  if (/^[a-z0-9-]+$/i.test(cleaned) && !cleaned.includes('.')) {
    return `https://${cleaned}.supabase.co`;
  }

  // Ensure protocol
  if (!cleaned.startsWith('http://') && !cleaned.startsWith('https://')) {
    cleaned = `https://${cleaned}`;
  }

  try {
    const parsed = new URL(cleaned);
    return parsed.origin;
  } catch {
    return 'https://placeholder.supabase.co';
  }
}

const supabaseUrl = sanitizeSupabaseUrl(process.env.NEXT_PUBLIC_SUPABASE_URL);
const supabaseAnonKey = (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || 'placeholder-key').trim().replace(/^['"]+|['"]+$/g, '');

// Fail fast rather than letting a client be built from placeholder credentials,
// which otherwise surfaces later as confusing auth and PostgREST errors.
// Checked lazily inside the factories so importing this module stays safe
// during build and prerender.
const SUPABASE_CONFIG_MISSING =
  'Supabase configuration is missing. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY.';

/**
 * Returns a human-readable error when the Supabase env vars are absent, or
 * null when they are set. Lets the UI render a configuration error screen
 * instead of throwing from an effect (which blanks the whole app).
 */
export function getSupabaseConfigError(): string | null {
  const rawUrl = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim().replace(/^['"]+|['"]+$/g, '');
  const rawKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim().replace(/^['"]+|['"]+$/g, '');
  return rawUrl && rawKey ? null : SUPABASE_CONFIG_MISSING;
}

const getRequiredConfig = () => {
  const configError = getSupabaseConfigError();
  if (configError) {
    throw new Error(configError);
  }

  return { supabaseUrl, supabaseAnonKey };
};

// Client for browser (with auth helpers)
export const createBrowserClient = () => {
  const { supabaseUrl, supabaseAnonKey } = getRequiredConfig();
  return createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      autoRefreshToken: true,
      persistSession: true,
      detectSessionInUrl: true,
    },
  });
};

// Server-side client (for API routes)
export const createServerClient = () => {
  const { supabaseUrl, supabaseAnonKey } = getRequiredConfig();
  return createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
};

// Export singleton browser client
let browserClient: ReturnType<typeof createBrowserClient> | null = null;

export const getBrowserClient = () => {
  if (!browserClient) {
    browserClient = createBrowserClient();
  }
  return browserClient;
};