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

// Client for browser (with auth helpers)
export const createBrowserClient = () => {
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