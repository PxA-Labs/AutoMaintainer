import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import InlineAssist from "./InlineAssist";

const selection = {
  startLine: 1,
  startColumn: 1,
  endLine: 2,
  endColumn: 1,
  selectedCode: "x = 1",
  prefixCode: "",
  suffixCode: "",
};

function renderAssist(onClose = vi.fn()) {
  render(
    <InlineAssist
      filePath="app.py"
      selectionContext={selection}
      position={{ top: 40, left: 40 }}
      onClose={onClose}
      onAccept={vi.fn()}
      getBackendUrl={() => "http://backend.test"}
      repoUrl="acme/widgets"
    />
  );
  return onClose;
}

describe("InlineAssist", () => {
  it("is a native, non-modal, labelled dialog that takes focus", () => {
    renderAssist();
    const dialog = screen.getByRole("dialog", { name: "Inline Assist" });
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog.hasAttribute("open")).toBe(true);
    expect(dialog.getAttribute("aria-modal")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("textbox", { name: "Inline Assist instruction" })
    );
  });

  it("closes on Escape pressed inside one of its controls", () => {
    const onClose = renderAssist();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Inline Assist instruction" }), {
      key: "Escape",
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
