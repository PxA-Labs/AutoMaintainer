// @vitest-environment node
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ESLint } from "eslint";

/**
 * Guards the lint gate itself: eslint-config-next only enables a few
 * jsx-a11y rules as warnings, so these patterns used to pass `npm run lint`.
 */
const cwd = fileURLToPath(new URL("../..", import.meta.url));

async function a11yErrors(code: string): Promise<string[]> {
  const eslint = new ESLint({ cwd });
  const [result] = await eslint.lintText(code, {
    filePath: `${cwd}/src/components/__a11y_probe__.tsx`,
  });
  return result.messages
    .filter((m) => m.severity === 2 && m.ruleId?.startsWith("jsx-a11y/"))
    .map((m) => m.ruleId as string);
}

describe("eslint jsx-a11y gate", () => {
  it("errors on a clickable div without role or keyboard support", async () => {
    const rules = await a11yErrors(
      "export function P({ f }: { f: () => void }) { return <div onClick={f}>x</div>; }\n"
    );
    expect(rules).toEqual(
      expect.arrayContaining([
        "jsx-a11y/click-events-have-key-events",
        "jsx-a11y/no-static-element-interactions",
      ])
    );
  }, 60_000);

  it("errors on an unlabelled autofocus and an empty anchor", async () => {
    const rules = await a11yErrors(
      'export function P() { return <><input autoFocus /><a href="/x" /></>; }\n'
    );
    expect(rules).toEqual(
      expect.arrayContaining(["jsx-a11y/no-autofocus", "jsx-a11y/anchor-has-content"])
    );
  }, 60_000);

  it("accepts an accessible button", async () => {
    const rules = await a11yErrors(
      'export function P({ f }: { f: () => void }) { return <button type="button" onClick={f}>Go</button>; }\n'
    );
    expect(rules).toEqual([]);
  }, 60_000);
});
