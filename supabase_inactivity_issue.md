### Description

Under the free tier of Supabase, PostgreSQL databases are automatically paused/disabled after 7 days of inactivity (i.e., when no API requests, database queries, or connections occur). Since AutoMaintainer might experience periods of inactivity between agent execution runs, the database can easily be auto-paused by Supabase.

When the database is paused, the FastAPI backend fails to write/insert execution logs or track active runs. Although backend telemetry writing handles errors gracefully (printing `Supabase insert failed` or `Failed to create run in Supabase`), it fails silently from a user standpoint—resulting in a completely blank dashboard UI and zero historical state tracking during execution. Fixing this requires manual admin intervention (logging into the Supabase console to manually restore the project).

### Proposed Implementation Architecture

To ensure high availability and prevent autonomous system degradation, we must implement a keep-alive/ping mechanism along with better status indication:

1. **Keep-Alive Mechanism (Two Alternative Paths):**
   * **Path A: Scheduled GitHub Action (Recommended for zero-runtime cost):**
     Create a workflow `.github/workflows/supabase-keepalive.yml` configured to run on a cron schedule (e.g., every 3 days). This workflow will execute a lightweight python script or curl request targeting the Supabase API/REST endpoints (or trigger a database ping query) using the repository secrets `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
   * **Path B: FastAPI Background Task:**
     Implement an async scheduler inside `backend/main.py` (e.g., using `FastAPI` startup events or a background thread) that queries the database (`SELECT 1;`) once every 48 hours to maintain active status.

2. **Supabase Connection Health Checks:**
   * Add a `/healthz/supabase` check endpoint on the FastAPI backend.
   * Upon backend boot, perform a quick verification check on the Supabase client connection. If it fails, log a critical warning specifying if the database might be paused.

3. **Frontend Diagnostics & Feedback:**
   * In the Next.js frontend (`dashboard/src/app/page.tsx`), if Supabase is offline or fails to load logs, display a user-friendly banner warning: *"Supabase Database is unreachable or paused. Historical logs and persistence are currently disabled."*

### Acceptance Criteria

1. **Keep-Alive Execution:** The keep-alive mechanism must run successfully without crashing and effectively keep the database from pausing. If using a GitHub Action, the workflow must be valid and testable.
2. **Robust Initialization:** If the Supabase client credentials are valid but the connection fails (e.g. database is currently paused), the application must not crash on start. It must log the connection error gracefully.
3. **Health Check Endpoint:** The `/healthz/supabase` endpoint must return a `200 OK` (when connected) or `503 Service Unavailable` with details (when paused or unreachable).
4. **Professionalism Constraints:** No emojis should be used in the commit messages, PR descriptions, issue comments, or source code modifications.

### Acceptance Approach

When reviewing the implementation for this issue, the reviewer will follow these validation steps:

1. **Workflow/Task Code Verification:** Check that the cron pattern is correctly configured to run at least twice a week.
2. **Failure Injection Test:** Simulate a database connection failure (e.g., by temporarily modifying the `SUPABASE_URL` to point to a non-existent host) and verify:
   * The backend starts successfully with clean console error logging.
   * The frontend doesn't hang indefinitely and displays an appropriate warnings banner to the user.
3. **API Validation:** Query the `/healthz/supabase` endpoint under healthy and simulated error states to verify correct response statuses.
