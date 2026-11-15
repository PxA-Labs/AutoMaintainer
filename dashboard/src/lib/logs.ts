/**
 * Bounded log buffers.
 *
 * A long agent run can emit tens of thousands of log rows over realtime. Keeping
 * them all in React state makes every append O(n) and renders one DOM row per
 * entry, which eventually freezes the tab. The dashboard only ever needs the
 * recent tail, so every log/activity array is capped through these helpers.
 */

/** Maximum number of log lines kept in memory and rendered. */
export const MAX_LOG_ENTRIES = 5000;

/** Maximum number of activity items kept (the feed only renders a handful). */
export const MAX_ACTIVITY_ENTRIES = 50;

/** Append `items` to `prev`, keeping only the newest `max` entries. */
export function appendCapped<T>(prev: T[], items: T[], max: number = MAX_LOG_ENTRIES): T[] {
  if (items.length === 0) return prev;
  const next = prev.concat(items);
  return next.length > max ? next.slice(next.length - max) : next;
}

/** Prepend `item` to `prev` (newest first), keeping only `max` entries. */
export function prependCapped<T>(prev: T[], item: T, max: number = MAX_ACTIVITY_ENTRIES): T[] {
  const next = [item, ...prev];
  return next.length > max ? next.slice(0, max) : next;
}
