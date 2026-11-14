/**
 * Width/height percentage of `value` relative to `max`, safe for empty data:
 * a zero or missing max yields 0 rather than NaN/Infinity, and the result is
 * clamped to [0, 100].
 */
export function barPercent(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) return 0;
  return Math.min(100, Math.max(0, (value / max) * 100));
}
