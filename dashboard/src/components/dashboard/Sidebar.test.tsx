import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Sidebar } from "@/components/dashboard/Sidebar";

vi.mock("next/link", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/ui/UserMenu", () => ({ UserMenu: () => null }));

function renderSidebar(onRepoUrlChange = vi.fn(), onSetIsEditingRepo = vi.fn()) {
  render(
    <Sidebar
      user={null}
      onSignOut={() => {}}
      activeTab="dashboard"
      onSelectTab={() => {}}
      repoUrl="octo/hello"
      onRepoUrlChange={onRepoUrlChange}
      isEditingRepo
      onSetIsEditingRepo={onSetIsEditingRepo}
      targetIssue=""
      onTargetIssueChange={() => {}}
      agentStatus={{}}
    />
  );
  return { onRepoUrlChange, onSetIsEditingRepo };
}

describe("Sidebar repository input", () => {
  it("commits the repository only on Enter, not per keystroke", () => {
    const { onRepoUrlChange } = renderSidebar();
    const input = screen.getByLabelText(/repository identifier/i);
    fireEvent.change(input, { target: { value: "octo/hello2" } });
    fireEvent.change(input, { target: { value: "octo/world" } });
    expect(onRepoUrlChange).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    expect(onRepoUrlChange).toHaveBeenCalledTimes(1);
    expect(onRepoUrlChange).toHaveBeenCalledWith("octo/world");
  });

  it("discards the draft on Escape", () => {
    const { onRepoUrlChange, onSetIsEditingRepo } = renderSidebar();
    const input = screen.getByLabelText(/repository identifier/i);
    fireEvent.change(input, { target: { value: "octo/other" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onRepoUrlChange).not.toHaveBeenCalled();
    expect(onSetIsEditingRepo).toHaveBeenCalledWith(false);
  });
});
