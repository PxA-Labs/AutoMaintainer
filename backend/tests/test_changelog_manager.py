"""Tests for scripts/changelog_manager.py (the auto-changelog workflow helper)."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "changelog_manager.py"
_spec = importlib.util.spec_from_file_location("changelog_manager", _SCRIPT)
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

LINKS = (
    "[Unreleased]: https://github.com/PxA-Labs/AutoMaintainer/compare/v1.0.0...HEAD\n"
    "[1.0.0]: https://github.com/PxA-Labs/AutoMaintainer/releases/tag/v1.0.0\n"
)

BASE = (
    "# Changelog\n\n---\n\n## [Unreleased]\n\n### Bug Fixes\n\n- old fix ([#1](u))\n\n"
    "---\n\n## [1.0.0] - 2026-06-20\n\n- initial\n"
)


@pytest.fixture
def changelog(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(BASE, encoding="utf-8")
    return p


def test_compare_links_are_idempotent(changelog):
    cm.update_compare_links(str(changelog))
    once = changelog.read_text(encoding="utf-8")
    for _ in range(5):
        cm.update_compare_links(str(changelog))
    assert changelog.read_text(encoding="utf-8") == once
    assert once.endswith("- initial\n\n---\n\n" + LINKS)


def test_compare_links_collapse_existing_separator_runs(changelog):
    changelog.write_text(
        BASE.rstrip() + "\n\n---\n" * 14 + "\n" + LINKS, encoding="utf-8"
    )
    cm.update_compare_links(str(changelog))
    text = changelog.read_text(encoding="utf-8")
    assert text.count("\n---\n") == 3  # header rule, before 1.0.0, before links
    assert text.endswith("- initial\n\n---\n\n" + LINKS)


def test_entries_accumulate_in_unreleased(changelog):
    # The workflow now applies each merged PR on top of the existing sync
    # branch, so successive calls must keep earlier entries.
    assert cm.add_entry_to_changelog(
        str(changelog), "Bug Fixes", "- fix A ([#10](u))", 10
    )
    assert cm.add_entry_to_changelog(
        str(changelog), "Features", "- feat B ([#11](u))", 11
    )
    cm.update_compare_links(str(changelog))
    text = changelog.read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]", 1)[1].split("## [1.0.0]", 1)[0]
    for tag in ("[#1]", "[#10]", "[#11]"):
        assert tag in unreleased
    assert not cm.add_entry_to_changelog(
        str(changelog), "Bug Fixes", "- fix A ([#10](u))", 10
    )
