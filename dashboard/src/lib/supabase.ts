import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || 'https://placeholder.supabase.co';
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || 'placeholder-key';

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

// Service role client (for admin operations)
export const createServiceClient = () => {
  const serviceKey = process.env.SUPABASE_SERVICE_KEY;
  if (!serviceKey) {
    throw new Error('SUPABASE_SERVICE_KEY not configured');
  }
  return createClient(supabaseUrl, serviceKey, {
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