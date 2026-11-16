"""Static checks on the GitHub Actions workflows (#406, #410).

These guard CI hygiene that used to regress silently:

- every third-party action is pinned to a full 40-character commit SHA with a
  ``# vX.Y.Z`` comment, so Dependabot can bump it and a reviewer can read it;
- every ``github/codeql-action`` step runs the same SHA (mismatched init and
  analyze versions made CodeQL fail on main).
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
# Disabled workflows (``*.yml.disabled``) are checked too: re-enabling one
# should not bring back an unpinned action.
WORKFLOWS = sorted(
    p
    for p in WORKFLOWS_DIR.iterdir()
    if p.name.endswith((".yml", ".yaml", ".yml.disabled"))
)

USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED_RE = re.compile(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}")
VERSION_COMMENT_RE = re.compile(r"\s+# v\d+\.\d+\.\d+\s*$")


def _uses_lines():
    for wf in WORKFLOWS:
        for lineno, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = USES_RE.match(line)
            if m:
                yield wf.name, lineno, m.group("ref"), m.group("rest")


def test_workflows_found():
    assert len(WORKFLOWS) >= 10


@pytest.mark.parametrize(
    "ref, ok",
    [
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", True),
        ("github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066", True),
        ("actions/checkout@v4", False),
        # One character too long: GitHub cannot resolve it as a commit SHA.
        ("actions/first-interaction@753c925c8d1ac6fede23781875376600628d9b5d3", False),
        ("actions/checkout@3d3c42e5", False),
    ],
)
def test_pin_pattern(ref, ok):
    assert bool(PINNED_RE.fullmatch(ref)) is ok


def test_every_action_is_pinned_to_a_full_sha_with_a_version_comment():
    bad = []
    for name, lineno, ref, rest in _uses_lines():
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not PINNED_RE.fullmatch(ref) or not VERSION_COMMENT_RE.fullmatch(rest):
            bad.append(f"{name}:{lineno}: {ref}{rest}")
    assert not bad, "unpinned or uncommented actions:\n" + "\n".join(bad)


def test_same_action_sha_always_has_the_same_version_comment():
    seen = {}
    for name, lineno, ref, rest in _uses_lines():
        if "@" not in ref:
            continue
        sha = ref.split("@", 1)[1]
        seen.setdefault(sha, set()).add(rest.strip())
    conflicting = {sha: c for sha, c in seen.items() if len(c) > 1}
    assert not conflicting


def test_all_codeql_action_steps_use_one_sha():
    shas = {
        ref.split("@", 1)[1]
        for _, _, ref, _ in _uses_lines()
        if ref.startswith("github/codeql-action/")
    }
    # init, autobuild and analyze in codeql-analysis.yml, upload-sarif in
    # scorecard.yml.
    assert len(shas) == 1, shas


def test_workflows_are_valid_yaml():
    for wf in WORKFLOWS:
        data = yaml.safe_load(wf.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and "jobs" in data, wf.name
