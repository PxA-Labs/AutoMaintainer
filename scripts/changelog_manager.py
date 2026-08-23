#!/usr/bin/env python3
"""
AutoMaintainer Changelog & Release Manager
Enterprise-grade changelog parser, formatter, and release synchronizer.
Adheres strictly to Keep a Changelog 1.1.0 and Conventional Commits 1.0.0.
"""

import os
import re
import sys
import json
import argparse
from typing import Dict, List, Optional, Tuple


# Standard Keep a Changelog / Conventional Categories (Strictly no emojis)
CATEGORY_MAPPING = {
    "breaking": "Breaking Changes",
    "feat": "Features",
    "feature": "Features",
    "fix": "Bug Fixes",
    "bug": "Bug Fixes",
    "security": "Security & Governance",
    "sec": "Security & Governance",
    "perf": "Performance Improvements",
    "performance": "Performance Improvements",
    "refactor": "Refactoring",
    "webide": "Web IDE & Developer Experience",
    "terminal": "Web IDE & Developer Experience",
    "monaco": "Web IDE & Developer Experience",
    "gitnexus": "Web IDE & Developer Experience",
    "ci": "CI/CD & Infrastructure",
    "deploy": "CI/CD & Infrastructure",
    "docker": "CI/CD & Infrastructure",
    "infra": "CI/CD & Infrastructure",
    "docs": "Documentation & Blueprints",
    "doc": "Documentation & Blueprints",
    "deps": "Dependency Updates & Maintenance",
    "chore": "Dependency Updates & Maintenance",
    "test": "Testing & Quality Assurance",
    "revert": "Architecture & Core Agent Platform",
}

SECTION_ORDER = [
    "Breaking Changes",
    "Features",
    "Web IDE & Developer Experience",
    "Security & Governance",
    "Performance Improvements",
    "Bug Fixes & System Stability",
    "Refactoring",
    "Architecture & Core Agent Platform",
    "CI/CD & Infrastructure",
    "Documentation & Blueprints",
    "Testing & Quality Assurance",
    "Dependency Updates & Maintenance",
]


def parse_conventional_pr(
    title: str,
    number: str | int,
    url: str,
    author: str,
    body: str = "",
    repo_slug: str = "PxA-Labs/AutoMaintainer",
) -> Tuple[str, str, bool]:
    """
    Parses PR title, body, and metadata into a categorized, formatted changelog entry.
    Returns: (category, formatted_entry_line, is_breaking)
    """
    clean_author = "dependabot[bot]" if author == "app/dependabot" else author.lstrip("@")
    title_clean = title.strip()

    # Check for Breaking Change
    is_breaking = False
    if "!" in title.split(":")[0] or "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body:
        is_breaking = True

    # Extract Conventional Commit Type & Scope: feat(engine): message -> type='feat', scope='engine'
    conv_match = re.match(r"^([a-zA-Z0-9_-]+)(?:\(([^)]+)\))?(!)?:\s*(.+)$", title_clean)
    
    if conv_match:
        c_type = conv_match.group(1).lower()
        scope = conv_match.group(2)
        breaking_flag = conv_match.group(3)
        description = conv_match.group(4).strip()
        if breaking_flag:
            is_breaking = True
    else:
        c_type = "feat"
        scope = None
        description = title_clean

    # Categorize
    if is_breaking:
        category = "Breaking Changes"
    elif c_type in CATEGORY_MAPPING:
        category = CATEGORY_MAPPING[c_type]
    else:
        # Fallback category heuristics
        t_low = title_clean.lower()
        if "security" in t_low or "openssf" in t_low or "vulnerability" in t_low or "scorecard" in t_low:
            category = "Security & Governance"
        elif "fix" in t_low or "resolve" in t_low:
            category = "Bug Fixes & System Stability"
        elif "docs" in t_low or "readme" in t_low or "blueprint" in t_low:
            category = "Documentation & Blueprints"
        elif "deps" in t_low or "bump" in t_low:
            category = "Dependency Updates & Maintenance"
        elif "monaco" in t_low or "terminal" in t_low or "xterm" in t_low:
            category = "Web IDE & Developer Experience"
        elif "ci" in t_low or "deploy" in t_low or "docker" in t_low:
            category = "CI/CD & Infrastructure"
        else:
            category = "Architecture & Core Agent Platform"

    # Extract linked closed issues from PR body or title
    linked_issues = []
    for match in re.finditer(r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#([0-9]+)", body + " " + title, re.IGNORECASE):
        issue_num = match.group(1)
        if issue_num != str(number) and issue_num not in linked_issues:
            linked_issues.append(issue_num)

    issue_ref = ""
    if linked_issues:
        issue_links = [f"[#{iss}](https://github.com/{repo_slug}/issues/{iss})" for iss in linked_issues]
        issue_ref = f" (closes {', '.join(issue_links)})"

    # Format description
    scope_tag = f"**{scope}**: " if scope else ""
    # Capitalize first letter of description if lowercase
    if description:
        description = description[0].upper() + description[1:] if len(description) > 1 else description.upper()

    pr_link = f"[#{number}]({url})" if url else f"#{number}"
    author_tag = f" - @{clean_author}" if clean_author else ""

    entry_line = f"- {scope_tag}{description} ({pr_link}){issue_ref}{author_tag}"
    return category, entry_line, is_breaking


def add_entry_to_changelog(
    changelog_path: str,
    category: str,
    entry: str,
    pr_number: str | int,
    force: bool = False,
) -> bool:
    """
    Safely inserts the entry under ## [Unreleased] in the given category.
    Returns True if added, False if duplicate and skipped.
    """
    if not os.path.exists(changelog_path):
        content = (
            "# Changelog\n\n"
            "All notable changes to the AutoMaintainer autonomous AI software engineering platform will be documented in this file.\n\n"
            "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),\n"
            "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
            "---\n\n"
            "## [Unreleased]\n\n"
        )
    else:
        with open(changelog_path, "r", encoding="utf-8") as f:
            content = f.read()

    # Ensure ## [Unreleased] heading exists
    if "## [Unreleased]" not in content:
        lines = content.splitlines()
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if line.startswith("# ") and not inserted:
                new_lines.append("")
                new_lines.append("## [Unreleased]")
                inserted = True
        content = "\n".join(new_lines) + "\n"

    # Partition around [Unreleased]
    parts = content.split("## [Unreleased]", 1)
    header = parts[0] + "## [Unreleased]\n"
    rest = parts[1]

    # Find next version heading (e.g. ## [0.9.0])
    next_heading = re.search(r"\n##\s+\[?[0-9v]", rest)
    if next_heading:
        unreleased_section = rest[: next_heading.start()]
        rest_section = rest[next_heading.start() :]
    else:
        # Separate compare links if at the bottom
        compare_match = re.search(r"\n\[Unreleased\]:\s+https?://", rest)
        if compare_match:
            unreleased_section = rest[: compare_match.start()]
            rest_section = rest[compare_match.start() :]
        else:
            unreleased_section = rest
            rest_section = ""

    # Check for duplicate
    pr_tag = f"[#{pr_number}]"
    if pr_tag in unreleased_section and not force:
        print(f"PR #{pr_number} already present in [Unreleased] section. Skipping duplicate.")
        return False

    cat_header = f"### {category}"
    if cat_header in unreleased_section:
        cat_parts = unreleased_section.split(cat_header, 1)
        unreleased_section = (
            cat_parts[0] + cat_header + "\n\n" + entry + "\n" + cat_parts[1].lstrip("\n")
        )
    else:
        unreleased_section = (
            "\n### " + category + "\n\n" + entry + "\n" + unreleased_section.lstrip("\n")
        )

    updated_changelog = (
        header + "\n" + unreleased_section.strip() + "\n\n" + rest_section.strip()
    ).strip() + "\n"

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(updated_changelog)

    return True


def update_compare_links(
    changelog_path: str,
    repo_slug: str = "PxA-Labs/AutoMaintainer",
    latest_tag: str = "v1.0.0",
):
    """
    Appends or updates Keep a Changelog semantic comparison links at the bottom.
    """
    if not os.path.exists(changelog_path):
        return

    with open(changelog_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Remove existing compare block
    content = re.sub(r"\n\[Unreleased\]:\s+https?://[^\n]+", "", content)
    content = re.sub(r"\n\[[0-9a-zA-Z._-]+\]:\s+https?://[^\n]+", "", content)
    content = content.rstrip()

    compare_block = (
        f"\n\n---\n\n"
        f"[Unreleased]: https://github.com/{repo_slug}/compare/{latest_tag}...HEAD\n"
        f"[{latest_tag.lstrip('v')}]: https://github.com/{repo_slug}/releases/tag/{latest_tag}\n"
    )

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(content + compare_block)


def extract_release_notes(changelog_path: str, tag: str) -> str:
    """
    Extracts the markdown release notes for a specific version tag.
    """
    if not os.path.exists(changelog_path):
        return ""

    with open(changelog_path, "r", encoding="utf-8") as f:
        content = f.read()

    version = tag.lstrip("v")
    pattern = rf"##\s+\[?v?{re.escape(version)}\]?[^\n]*\n(.*?)(?=\n##\s+\[|\n\[Unreleased\]:|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description="AutoMaintainer Changelog & Release Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Add entry command
    add_parser = subparsers.add_parser("add", help="Add a PR entry to CHANGELOG.md")
    add_parser.add_argument("--file", default="CHANGELOG.md", help="Path to CHANGELOG.md")
    add_parser.add_argument("--title", required=True, help="PR Title")
    add_parser.add_argument("--number", required=True, help="PR Number")
    add_parser.add_argument("--url", default="", help="PR URL")
    add_parser.add_argument("--author", default="", help="PR Author handle")
    add_parser.add_argument("--body", default="", help="PR Description body")
    add_parser.add_argument("--force", action="store_true", help="Force add even if duplicate")
    add_parser.add_argument("--repo", default="PxA-Labs/AutoMaintainer", help="GitHub repo slug")

    # Extract release notes command
    extract_parser = subparsers.add_parser("extract-notes", help="Extract release notes for a version tag")
    extract_parser.add_argument("--file", default="CHANGELOG.md", help="Path to CHANGELOG.md")
    extract_parser.add_argument("--tag", required=True, help="Version tag (e.g. v1.0.0)")

    args = parser.parse_args()

    if args.command == "add":
        category, entry_line, is_breaking = parse_conventional_pr(
            title=args.title,
            number=args.number,
            url=args.url,
            author=args.author,
            body=args.body,
            repo_slug=args.repo,
        )
        added = add_entry_to_changelog(
            changelog_path=args.file,
            category=category,
            entry=entry_line,
            pr_number=args.number,
            force=args.force,
        )
        if added:
            update_compare_links(args.file, repo_slug=args.repo)
            print(f"Added to '{category}': {entry_line}")
        else:
            print("No updates made (entry already exists).")

    elif args.command == "extract-notes":
        notes = extract_release_notes(args.file, args.tag)
        print(notes)


if __name__ == "__main__":
    main()
