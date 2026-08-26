import 'server-only';

import { createClient } from '@supabase/supabase-js';

/**
 * Service role client for admin/server operations.
 *
 * This module is guarded by `server-only`, so any attempt to import it from a
 * Client Component fails the build instead of silently bundling the service
 * role key into client-side JavaScript.
 */
export const createServiceClient = () => {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_KEY;

  if (!supabaseUrl || !serviceKey) {
    throw new Error(
      'SUPABASE_SERVICE_KEY / NEXT_PUBLIC_SUPABASE_URL not configured (server environment only)'
    );
  }

  return createClient(supabaseUrl, serviceKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
};
