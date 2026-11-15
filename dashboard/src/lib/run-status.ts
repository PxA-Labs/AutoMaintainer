/** Run statuses (runs.status) during which a run is still making progress. */
export const ACTIVE_RUN_STATUSES = ["queued", "running"] as const;

/**
 * True once a run can no longer make progress: completed, failed, cancelled,
 * or any status the backend adds later that is not explicitly active.
 */
export function isTerminalRunStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return !(ACTIVE_RUN_STATUSES as readonly string[]).includes(status);
}

/** True only for a status that is explicitly queued or running. */
export function isActiveRunStatus(status: string | null | undefined): boolean {
  return !!status && (ACTIVE_RUN_STATUSES as readonly string[]).includes(status);
}

/** The subset of a GET /runs row needed to offer resuming a run. */
export interface RunSummary {
  id: string;
  status: string;
  repo_name?: string | null;
}

/**
 * The most recent queued/running run for `repoName`, if any.
 *
 * GET /runs returns rows newest first, so the first match is the latest one.
 * Repository names are compared case-insensitively, as GitHub does.
 */
export function findResumableRun<T extends RunSummary>(
  runs: readonly T[],
  repoName: string
): T | null {
  const wanted = repoName.trim().toLowerCase();
  if (!wanted) return null;
  return (
    runs.find(
      (run) =>
        isActiveRunStatus(run.status) &&
        (run.repo_name ?? "").toLowerCase() === wanted
    ) ?? null
  );
}
