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
