import { beforeEach, describe, expect, it } from "vitest";
import {
  consumeReturnTo,
  rememberReturnTo,
  RETURN_TO_STORAGE_KEY,
  safeReturnPath,
} from "@/lib/auth-return";

describe("sign-in return path", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("remembers the current path, query and hash, and forgets it once read", () => {
    window.history.replaceState(null, "", "/?run=abc-123#logs");
    rememberReturnTo();
    expect(consumeReturnTo()).toBe("/?run=abc-123#logs");
    expect(consumeReturnTo()).toBe("/");
  });

  it("falls back to / when nothing was remembered", () => {
    expect(consumeReturnTo()).toBe("/");
  });

  it("does not remember the callback page itself", () => {
    window.history.replaceState(null, "", "/auth/callback/?code=x");
    rememberReturnTo();
    expect(window.sessionStorage.getItem(RETURN_TO_STORAGE_KEY)).toBeNull();
  });

  it.each([
    "https://evil.example/",
    "//evil.example/",
    "/\\evil.example/",
    "javascript:alert(1)",
    "/auth/callback/",
    "/" + "a".repeat(5000),
    // The URL parser strips tabs and newlines, turning these into "//evil".
    "/\t/evil.example/",
    "/\n/evil.example/",
    "/\r\n/evil.example/",
    // Dot segments that resolve back to the callback.
    "/x/../auth/callback/",
  ])("never returns to %s", (stored) => {
    window.sessionStorage.setItem(RETURN_TO_STORAGE_KEY, stored);
    expect(consumeReturnTo()).toBe("/");
    expect(safeReturnPath(stored)).toBeNull();
  });
});
