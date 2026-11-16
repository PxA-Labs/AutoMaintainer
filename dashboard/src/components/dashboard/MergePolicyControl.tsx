"use client";

import { useEffect, useId, useState } from "react";
import { GitMerge } from "lucide-react";
import {
  fetchMergePolicy,
  isWithinCeiling,
  MERGE_POLICIES,
  MERGE_POLICY_LABELS,
  MergePolicyError,
  updateMergePolicy,
  type MergePolicy,
  type RepositoryMergePolicy,
} from "@/lib/merge-policy";

export interface MergePolicyRepository {
  id: number;
  full_name: string;
}

function message(err: unknown, fallback: string): string {
  return err instanceof MergePolicyError ? err.message : fallback;
}

function Panel({ repository }: { repository: MergePolicyRepository }) {
  const selectId = useId();
  const [policy, setPolicy] = useState<RepositoryMergePolicy | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  // Remounted per repository (keyed by id), so a late answer for another
  // repository can never land here; `active` covers unmounting.
  useEffect(() => {
    let active = true;
    fetchMergePolicy(repository.id).then(
      (next) => {
        if (active) setPolicy(next);
      },
      (err: unknown) => {
        if (active) setLoadError(message(err, "Could not load the merge policy."));
      }
    );
    return () => {
      active = false;
    };
  }, [repository.id]);

  const change = async (value: string) => {
    const setting = value === "" ? null : (value as MergePolicy);
    setSaving(true);
    setSaved(false);
    setSaveError(null);
    try {
      setPolicy(await updateMergePolicy(repository.id, setting));
      setSaved(true);
    } catch (err) {
      setSaveError(message(err, "Could not save the merge policy."));
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <p role="alert" className="text-red-400">
        {loadError}
      </p>
    );
  }
  if (!policy) return <p className="text-muted">Loading merge policy...</p>;

  const canEdit = policy.can_edit;
  return (
    <>
      <label htmlFor={selectId} className="text-[10px] font-medium text-muted flex items-center gap-1.5">
        <GitMerge className="w-3 h-3 text-zinc-400" aria-hidden="true" />
        Merge policy
      </label>
      <select
        id={selectId}
        value={policy.setting ?? ""}
        disabled={!canEdit || saving}
        aria-busy={saving}
        aria-describedby={`${selectId}-help`}
        onChange={(e) => void change(e.target.value)}
        className="w-full bg-zinc-950 border border-zinc-800/80 rounded px-2 py-1 text-xs text-zinc-100 focus:outline-none focus:border-indigo-500 disabled:opacity-60"
      >
        <option value="">Inherit ({MERGE_POLICY_LABELS[policy.ceiling]})</option>
        {MERGE_POLICIES.map((p) => {
          const allowed = isWithinCeiling(p, policy.ceiling);
          return (
            <option key={p} value={p} disabled={!allowed}>
              {MERGE_POLICY_LABELS[p]}
              {allowed ? "" : " (above deployment limit)"}
            </option>
          );
        })}
      </select>
      <p id={`${selectId}-help`} className="text-[10px] text-muted">
        Deployment limit: {MERGE_POLICY_LABELS[policy.ceiling]}. Effective:{" "}
        <span className="text-zinc-300">{MERGE_POLICY_LABELS[policy.effective]}</span>.
        {canEdit ? "" : " Only organization owners and admins can change it."}
      </p>
      <p role="status" className="sr-only">
        {saved ? `Merge policy saved. Effective: ${MERGE_POLICY_LABELS[policy.effective]}.` : ""}
      </p>
      {saveError && (
        <p role="alert" className="text-red-400">
          {saveError}
        </p>
      )}
    </>
  );
}

/**
 * The selected repository's merge policy: its own setting (or inherit), the
 * deployment's MERGE_POLICY ceiling and the effective policy. Options looser
 * than the ceiling are disabled; the backend refuses them too, and only org
 * owners and admins may change the setting.
 */
export function MergePolicyControl({ repository }: { repository: MergePolicyRepository | null }) {
  if (!repository) return null;
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/60 rounded-lg p-2 space-y-1.5 text-xs">
      <Panel key={repository.id} repository={repository} />
    </div>
  );
}
