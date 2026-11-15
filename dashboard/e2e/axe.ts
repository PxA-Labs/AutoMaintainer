import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";
import type { Result } from "axe-core";

/** WCAG 2.0 and 2.1, levels A and AA. */
export const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

/** Impacts that fail the build; minor/moderate are reported by axe but not gated. */
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

/**
 * A known violation that is accepted for one rule on one element. Keep
 * entries narrow (a rule plus a selector, never a whole rule) and give the
 * reason inline so it is reviewed whenever the list changes.
 */
export interface AxeAllowance {
  rule: string;
  /** Exact axe target selector of the accepted node (as printed in the failure). */
  target: string;
  reason: string;
}

function describe(violations: Result[]) {
  return violations
    .map((v) => {
      const nodes = v.nodes
        .map((n) => `    - ${n.target.join(" ")}\n      ${n.failureSummary?.replace(/\n/g, "\n      ")}`)
        .join("\n");
      return `[${v.impact}] ${v.id}: ${v.help}\n  ${v.helpUrl}\n${nodes}`;
    })
    .join("\n\n");
}

/**
 * Run axe against the current state of `page` and fail on any serious or
 * critical WCAG 2.1 A/AA violation that is not explicitly allowed.
 */
export async function expectNoA11yViolations(page: Page, screen: string, allow: AxeAllowance[] = []) {
  // Let finite entrance transitions finish so contrast is measured on the
  // final colours rather than a half-faded frame. Infinite ones (pulsing
  // status dots, spinners) never resolve `finished`, so they are skipped,
  // and the wait is capped so a paused or throttled animation (a loaded CI
  // runner) cannot hang the test; axe then simply runs on what is shown.
  await page.evaluate(() =>
    Promise.race([
      Promise.all(
        document
          .getAnimations()
          .filter((a) => Number.isFinite(Number(a.effect?.getComputedTiming().endTime)))
          .map((a) => a.finished.catch(() => undefined))
      ),
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ])
  );

  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();

  const blocking = results.violations
    .filter((v) => v.impact && BLOCKING_IMPACTS.has(v.impact))
    .map((v) => ({
      ...v,
      nodes: v.nodes.filter(
        (n) => !allow.some((a) => a.rule === v.id && n.target.join(" ") === a.target)
      ),
    }))
    .filter((v) => v.nodes.length > 0);

  // Compare a compact summary so a failure prints the readable report in the
  // message instead of a diff of the raw axe result objects.
  const summary = blocking.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(summary, `axe violations on ${screen}:\n\n${describe(blocking)}`).toEqual([]);
}
