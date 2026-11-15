import { describe, expect, it } from "vitest";
import { findResumableRun, isActiveRunStatus } from "@/lib/run-status";

describe("findResumableRun", () => {
  // GET /runs order: newest first.
  const runs = [
    { id: "r5", status: "completed", repo_name: "octo/a" },
    { id: "r4", status: "running", repo_name: "octo/b" },
    { id: "r3", status: "queued", repo_name: "Octo/A" },
    { id: "r2", status: "running", repo_name: "octo/a" },
  ];

  it("returns the newest queued/running run for the repository", () => {
    expect(findResumableRun(runs, "octo/a")?.id).toBe("r3");
    expect(findResumableRun(runs, " octo/b ")?.id).toBe("r4");
  });

  it("returns null when nothing is active for the repository", () => {
    expect(findResumableRun(runs, "octo/c")).toBeNull();
    expect(findResumableRun(runs, "")).toBeNull();
    expect(findResumableRun([{ id: "x", status: "failed", repo_name: "octo/a" }], "octo/a")).toBeNull();
  });
});

describe("isActiveRunStatus", () => {
  it("is true only for queued and running", () => {
    expect(isActiveRunStatus("queued")).toBe(true);
    expect(isActiveRunStatus("running")).toBe(true);
    expect(isActiveRunStatus("completed")).toBe(false);
    expect(isActiveRunStatus(undefined)).toBe(false);
  });
});
