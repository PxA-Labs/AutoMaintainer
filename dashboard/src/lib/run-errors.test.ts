import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { describeRunError, formatRunErrorLog, KNOWN_RUN_ERROR_CODES } from "@/lib/run-errors";

// The codes are read from the backend source instead of a copied list, so a
// new backend code fails this test until it has a label. Neither the OpenAPI
// snapshot nor any shared file lists them (they only appear in ErrorEvents).
// backend/tests/contract/test_dashboard_error_codes.py makes the same check
// from the backend side (with the AST), so a backend-only change is caught too.
const AGENTS_PY = resolve(import.meta.dirname, "../../../backend/agents.py");

function backendErrorCodes(source: string): Set<string> {
  const patterns = [
    /error_code="([A-Z][A-Z0-9_]*)"/g, // ErrorEvent(error_code="...")
    /PipelineAbortError\(\s*"([A-Z][A-Z0-9_]*)"/g, // raise PipelineAbortError("...", ...)
    /_fail_run\(\s*\w+,\s*"([A-Z][A-Z0-9_]*)"/g, // _fail_run(run_id, "...", ...)
  ];
  const codes = new Set<string>();
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) codes.add(match[1]);
  }
  return codes;
}

describe("run error codes", () => {
  it.skipIf(!existsSync(AGENTS_PY))("has a label for every code the backend emits", () => {
    const codes = backendErrorCodes(readFileSync(AGENTS_PY, "utf8"));
    // Guard against the extraction silently matching nothing after a refactor.
    for (const code of [
      "REPO_CLONE_FAILED",
      "REPO_SYNC_FAILED",
      "INVALID_TARGET_ISSUE",
      "PROTECTED_PATH",
      "RUN_FAILED",
      "RATE_LIMIT_REACHED",
    ]) {
      expect(codes).toContain(code);
    }
    const missing = [...codes].filter((code) => !KNOWN_RUN_ERROR_CODES.includes(code));
    expect(missing).toEqual([]);
  });

  it("has a non-empty label and help text for every entry", () => {
    for (const code of KNOWN_RUN_ERROR_CODES) {
      const info = describeRunError(code);
      expect(info.label.trim(), code).not.toBe("");
      expect(info.help.trim(), code).not.toBe("");
    }
  });

  it.each(["REPO_CLONE_FAILED", "REPO_SYNC_FAILED", "INVALID_TARGET_ISSUE", "PROTECTED_PATH"])(
    "labels %s",
    (code) => {
      const info = describeRunError(code);
      expect(info.code).toBe(code);
      expect(info.label).not.toBe("Unrecognised error");
    }
  );

  it("describes an unknown but well-formed code generically and keeps the code", () => {
    expect(describeRunError("BRAND_NEW_CODE")).toMatchObject({
      code: "BRAND_NEW_CODE",
      label: "Unrecognised error",
    });
  });

  it.each([
    [undefined],
    [null],
    [42],
    [{}],
    ["<img src=x onerror=alert(1)>"],
    ["toString"],
    ["__proto__"],
    ["A".repeat(200)],
  ])("does not echo a malformed code (%s)", (code) => {
    const info = describeRunError(code);
    expect(info.code).toBe("UNKNOWN");
    expect(info.label).toBe("Unrecognised error");
  });

  it("shows REPO_SYNC_FAILED as a warning, not a run failure", () => {
    const log = formatRunErrorLog(
      { error_code: "REPO_SYNC_FAILED", message: "using the cached copy", fatal: false },
      "Error [REPO_SYNC_FAILED]: using the cached copy"
    );
    expect(log.msg).toBe("Warning: Repository update failed [REPO_SYNC_FAILED]: using the cached copy");
    expect(log.color).toBe("text-amber-400");
    expect(log.help).toMatch(/Not a run failure/);
  });

  it("shows a fatal error in red, falling back to the row message", () => {
    const log = formatRunErrorLog({ error_code: "REPO_CLONE_FAILED", fatal: true }, "clone failed");
    expect(log.msg).toBe("Repository clone failed [REPO_CLONE_FAILED]: clone failed");
    expect(log.color).toBe("text-red-400");
  });

  it("tolerates missing metadata", () => {
    expect(formatRunErrorLog(null, "boom")).toMatchObject({
      msg: "Unrecognised error [UNKNOWN]: boom",
      color: "text-red-400",
    });
  });
});
