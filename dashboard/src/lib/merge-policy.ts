/**
 * Per-repository merge policy (backend: GET/PUT
 * /repositories/{id}/merge-policy, backend/merge_policy.py).
 *
 * The deployment's MERGE_POLICY is the ceiling; a repository can only make
 * it stricter (human < ci < auto) or inherit it (null). The backend computes
 * the effective policy and refuses a setting above the ceiling (422); the
 * dashboard mirrors that by disabling those options.
 */

import { authedFetch } from "@/lib/api";
import { normalizeRepoSlug } from "@/lib/repo";

/**
 * The org repository (from the GitHub App state) the sidebar's repository
 * identifier names, or null when it is not one of the org's repositories.
 * GitHub names are case-insensitive.
 */
export function findOrgRepository<T extends { id: number; full_name: string }>(
  repositories: readonly T[] | null | undefined,
  repoUrl: string
): T | null {
  const slug = normalizeRepoSlug(repoUrl)?.toLowerCase();
  if (!slug || !repositories) return null;
  return repositories.find((r) => r.full_name.toLowerCase() === slug) ?? null;
}

export const MERGE_POLICIES = ["human", "ci", "auto"] as const;
export type MergePolicy = (typeof MERGE_POLICIES)[number];

export const MERGE_POLICY_LABELS: Record<MergePolicy, string> = {
  human: "Human review",
  ci: "CI-gated",
  auto: "Automatic",
};

export interface RepositoryMergePolicy {
  repository_id: number;
  full_name: string | null;
  /** The repository's own setting; null inherits the ceiling. */
  setting: MergePolicy | null;
  ceiling: MergePolicy;
  effective: MergePolicy;
  /** Settings the API accepts (those not looser than the ceiling). */
  allowed: MergePolicy[];
  /** Whether the caller may change it (org owners and admins). */
  can_edit: boolean;
}

/** Whether `policy` is stricter than or equal to `ceiling`. */
export function isWithinCeiling(policy: MergePolicy, ceiling: MergePolicy): boolean {
  return MERGE_POLICIES.indexOf(policy) <= MERGE_POLICIES.indexOf(ceiling);
}

export class MergePolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MergePolicyError";
  }
}

async function failure(res: Response, action: string): Promise<MergePolicyError> {
  let detail: string | null = null;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
  } catch {
    // No JSON body.
  }
  if (res.status === 403) {
    return new MergePolicyError(detail ?? "Only organization owners and admins can change the merge policy.");
  }
  if (res.status === 404) {
    return new MergePolicyError("This repository is not linked to your organization.");
  }
  return new MergePolicyError(
    detail ? `Could not ${action} the merge policy: ${detail}` : `Could not ${action} the merge policy (HTTP ${res.status}).`
  );
}

export async function fetchMergePolicy(repositoryId: number): Promise<RepositoryMergePolicy> {
  const res = await authedFetch(`/repositories/${repositoryId}/merge-policy`);
  if (!res.ok) throw await failure(res, "load");
  return (await res.json()) as RepositoryMergePolicy;
}

/** Store `setting` (null = inherit); resolves to the updated policy. */
export async function updateMergePolicy(
  repositoryId: number,
  setting: MergePolicy | null
): Promise<RepositoryMergePolicy> {
  const res = await authedFetch(`/repositories/${repositoryId}/merge-policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ merge_policy: setting }),
  });
  if (!res.ok) throw await failure(res, "save");
  return (await res.json()) as RepositoryMergePolicy;
}
