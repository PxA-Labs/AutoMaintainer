-- 1. Create the Runs table
CREATE TABLE runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_name TEXT NOT NULL,
    target_issue INTEGER,
    branch_name TEXT,
    status TEXT DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 2. Create the Logs table
CREATE TABLE logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    log_type TEXT DEFAULT 'message',
    message TEXT,
    color TEXT,
    metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 3. Create Index for faster chronological queries
CREATE INDEX logs_run_id_created_at_idx ON logs(run_id, created_at ASC);

-- 4. Enable Row Level Security (RLS)
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs ENABLE ROW LEVEL SECURITY;

-- 5. Create RLS Policies
-- Allow anyone to read runs and logs (since this is an internal dashboard tool)
CREATE POLICY "Allow anonymous read access to runs" ON runs FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "Allow anonymous read access to logs" ON logs FOR SELECT TO anon, authenticated USING (true);

-- The backend will use the Service Role Key, which bypasses RLS, so we don't need to create INSERT policies for the backend.

-- 6. Enable Supabase Realtime for the 'logs' table
-- This allows the Next.js frontend to subscribe to new agent log inserts instantly.
BEGIN;
  DROP PUBLICATION IF EXISTS supabase_realtime;
  CREATE PUBLICATION supabase_realtime;
COMMIT;
ALTER PUBLICATION supabase_realtime ADD TABLE logs;
