import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Unmount rendered trees between tests (no globals mode, so not automatic).
afterEach(() => {
  cleanup();
});
