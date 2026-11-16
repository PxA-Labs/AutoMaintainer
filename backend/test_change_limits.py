"""Unit tests for change_limits.py: limits, globs, configuration, reporting."""

import pytest

import change_limits as cl
from change_limits import ChangeLimits, FileChange


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for name in (
        "CHANGE_LIMIT_MAX_FILES",
        "CHANGE_LIMIT_MAX_LINES",
        "CHANGE_LIMIT_DENYLIST",
    ):
        monkeypatch.delenv(name, raising=False)


def _violations(changes, limits=None):
    return cl.find_violations(changes, limits or cl.default_limits())


# --- Size limits ----------------------------------------------------------------


def test_small_change_within_default_limits():
    assert _violations([FileChange("src/app.py", 3, 1)]) == []


def test_max_files_is_a_strict_upper_bound():
    limits = ChangeLimits(max_files=2)
    two = [FileChange("a.py", 1), FileChange("b.py", 1)]
    assert cl.find_violations(two, limits) == []
    found = cl.find_violations(two + [FileChange("c.py", 1)], limits)
    assert found == ["3 files changed, above the limit of 2."]


def test_the_same_path_twice_counts_as_one_file():
    changes = [FileChange("a.py", 1), FileChange("a.py", 1)]
    assert cl.find_violations(changes, ChangeLimits(max_files=1)) == []


def test_max_lines_counts_additions_and_deletions():
    limits = ChangeLimits(max_lines=10)
    assert cl.find_violations([FileChange("a.py", 6, 4)], limits) == []
    found = cl.find_violations(
        [FileChange("a.py", 6, 3), FileChange("b.py", 2)], limits
    )
    assert found == ["11 lines changed, above the limit of 10."]


def test_count_line_changes_matches_a_unified_diff():
    old = "a\nb\nc\n"
    assert cl.count_line_changes(old, old) == (0, 0)
    assert cl.count_line_changes(old, "a\nB\nc\nd\n") == (2, 1)
    assert cl.count_line_changes("", "x\ny\n") == (2, 0)
    assert cl.count_line_changes(old, "") == (0, 3)


# --- Denylist and glob matching -------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/nested/deploy.yaml",
        "Dockerfile",
        "deploy/Dockerfile.prod",
        "docker-compose.yml",
        "infra/docker-compose.override.yml",
        "uv.lock",
        "backend/uv.lock",
        "package-lock.json",
        "dashboard/package-lock.json",
        "poetry.lock",
        "yarn.lock",
        "web/pnpm-lock.yaml",
        ".env",
        "config/.env.production",
        "certs/server.pem",
        "id_rsa.key",
        ".automaintainer.yml",
        ".GitHub/Workflows/CI.yml",  # case variants are denied too
    ],
)
def test_default_denylist_flags_sensitive_paths(path):
    found = _violations([FileChange(path, 1)])
    assert len(found) == 1 and "protected path pattern" in found[0]


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "docs/workflows.md",
        ".github/ISSUE_TEMPLATE/bug.md",
        "environment.py",
        "keyring.py",
        "docs/docker.md",
    ],
)
def test_default_denylist_leaves_ordinary_paths_alone(path):
    assert _violations([FileChange(path, 1)]) == []


# Test-runner configuration can skip or short-circuit the tests verification
# runs, so it is held for a human by default (#480).
@pytest.mark.parametrize(
    "path, pattern",
    [
        ("conftest.py", "conftest.py"),
        ("tests/conftest.py", "conftest.py"),
        ("backend/tests/unit/conftest.py", "conftest.py"),
        ("pytest.ini", "pytest.ini"),
        (".pytest.ini", ".pytest.ini"),
        ("tox.ini", "tox.ini"),
        ("noxfile.py", "noxfile.py"),
        ("setup.cfg", "setup.cfg"),
        ("pyproject.toml", "pyproject.toml"),
        ("backend/pyproject.toml", "pyproject.toml"),
        ("package.json", "package.json"),
        ("dashboard/package.json", "package.json"),
        ("jest.config.js", "jest.config.*"),
        ("web/jest.config.ts", "jest.config.*"),
        ("vitest.config.mts", "vitest.config.*"),
        (".mocharc.yml", ".mocharc*"),
        ("app/.mocharc", ".mocharc*"),
        ("karma.conf.js", "karma.conf.*"),
        ("e2e/playwright.config.ts", "playwright.config.*"),
    ],
)
def test_default_denylist_holds_test_runner_configuration(path, pattern):
    assert cl.denied_by(path, cl.default_limits()) == pattern


@pytest.mark.parametrize(
    "path",
    [
        # Test files stay writable: the agent legitimately adds and fixes tests.
        "tests/test_app.py",
        "test_app.py",
        "src/app.test.ts",
        "tests/fixtures/conftest_data.py",
        "docs/pyproject.md",
        "src/package_json.py",
        "jest.setup.js",
    ],
)
def test_default_denylist_leaves_test_files_alone(path):
    assert _violations([FileChange(path, 1)]) == []


@pytest.mark.parametrize(
    "path, pattern, expected",
    [
        ("a/b.py", "*.py", True),  # no "/": matches the name in any directory
        ("a/b.py", "a/*.py", True),
        ("x/a/b.py", "a/*.py", False),  # with "/": anchored at the root
        ("a/c/b.py", "a/*.py", False),  # "*" does not cross "/"
        ("a/b.py", "a/**/b.py", True),  # "**/" matches zero directories
        ("a/x/y/b.py", "a/**/b.py", True),
        ("a/x/y/b.py", "a/**", True),
        ("b/x.py", "a/**", False),
        ("a/b.py", "/a/b.py", True),  # a leading "/" is the repository root
        ("ab.py", "a?.py", True),
        ("a/b.py", "a?b.py", False),  # "?" does not match "/"
        ("a.py+", "a.py", False),  # regex metacharacters are literal
        ("aXpy", "a.py", False),
        ("A/B.PY", "a/b.py", True),
    ],
)
def test_glob_matching(path, pattern, expected):
    assert cl.path_matches(path, pattern) is expected


def test_rename_away_from_a_protected_path_is_flagged():
    found = _violations(
        [FileChange("ci.yml.bak", 0, 0, previous_path=".github/workflows/ci.yml")]
    )
    assert len(found) == 1 and ".github/workflows/ci.yml" in found[0]


def test_backticks_in_a_path_cannot_break_out_of_the_code_span():
    found = cl.find_violations(
        [FileChange(".env`` @admin ``", 1)], ChangeLimits(deny=(".env*",))
    )
    assert "``` .env`` @admin `` ```" in found[0]


# --- Configuration --------------------------------------------------------------


def test_missing_or_empty_config_uses_the_defaults():
    assert cl.limits_from_config(None) == cl.default_limits()
    assert cl.limits_from_config("") == cl.default_limits()
    assert cl.limits_from_config("test_command: [pytest]\n") == cl.default_limits()


def test_config_overrides_sizes_and_extends_lists():
    limits = cl.limits_from_config(
        "change_limits:\n"
        "  max_files: 3\n"
        "  max_lines: 40\n"
        "  deny: ['migrations/**']\n"
        "  allow: ['uv.lock']\n"
    )
    assert (limits.max_files, limits.max_lines) == (3, 40)
    assert _violations([FileChange("migrations/0001.py", 1)], limits)
    assert _violations([FileChange("backend/uv.lock", 1)], limits) == []
    # Allowing one lockfile does not allow the others.
    assert _violations([FileChange("poetry.lock", 1)], limits)


def test_allow_re_enables_test_runner_configuration_per_repository():
    """The escape hatch for repositories whose fixes routinely touch
    pyproject.toml: allow it on the base branch, keep the rest denied."""
    limits = cl.limits_from_config(
        "change_limits:\n  allow: ['pyproject.toml', 'tests/conftest.py']\n"
    )
    assert _violations([FileChange("backend/pyproject.toml", 1)], limits) == []
    assert _violations([FileChange("tests/conftest.py", 1)], limits) == []
    # An anchored allow entry exempts that file only.
    assert _violations([FileChange("conftest.py", 1)], limits)
    assert _violations([FileChange("tox.ini", 1)], limits)
    assert _violations([FileChange("package.json", 1)], limits)


def test_allow_cannot_exempt_the_automaintainer_config():
    limits = cl.limits_from_config(
        "change_limits:\n  allow: ['.automaintainer.yml', '**']\n"
    )
    assert _violations([FileChange(".automaintainer.yml", 1)], limits)
    assert _violations([FileChange("Dockerfile", 1)], limits) == []


@pytest.mark.parametrize(
    "text",
    [
        "change_limits: [1, 2]",
        "change_limits:\n  max_files: 0\n",
        "change_limits:\n  max_lines: -5\n",
        "change_limits:\n  max_lines: true\n",
        "change_limits:\n  max_lines: '100'\n",
        "change_limits:\n  deny: '.env'\n",
        "change_limits:\n  allow: [1]\n",
        "change_limits:\n  allow: ['']\n",
        "- just\n- a list\n",
        "not: [valid",
    ],
)
def test_invalid_config_is_an_error_not_a_silent_fallback(text):
    with pytest.raises(cl.ChangeLimitsConfigError):
        cl.limits_from_config(text)


def test_env_settings_override_the_built_in_defaults(monkeypatch):
    monkeypatch.setenv("CHANGE_LIMIT_MAX_FILES", "2")
    monkeypatch.setenv("CHANGE_LIMIT_MAX_LINES", "20")
    monkeypatch.setenv("CHANGE_LIMIT_DENYLIST", "secrets/**, *.sql")
    limits = cl.default_limits()
    assert (limits.max_files, limits.max_lines) == (2, 20)
    assert limits.deny == ("secrets/**", "*.sql")
    assert _violations([FileChange("db/schema.sql", 1)], limits)
    # Replacing the denylist drops the built-in entries ...
    assert _violations([FileChange("Dockerfile", 1)], limits) == []
    # ... but never the protection of the config file itself.
    assert _violations([FileChange(".automaintainer.yml", 1)], limits)
    # The repository config starts from the env defaults.
    assert cl.limits_from_config("change_limits:\n  max_files: 5\n").max_lines == 20


@pytest.mark.parametrize("value", ["", "abc", "0", "-3"])
def test_invalid_env_numbers_fall_back_to_the_defaults(monkeypatch, value):
    monkeypatch.setenv("CHANGE_LIMIT_MAX_FILES", value)
    assert cl.default_limits().max_files == cl.DEFAULT_MAX_FILES


# --- PR body section ------------------------------------------------------------


def test_pr_section_lists_violations_and_is_replaced_in_place():
    first = cl.render_pr_section(["one", "two"])
    assert "- one\n- two" in first and cl.NEEDS_HUMAN_LABEL in first
    body = cl.upsert_pr_section("Closes #1", first)
    assert body.startswith("Closes #1\n\n" + cl.SECTION_START)
    second = cl.upsert_pr_section(body, cl.render_pr_section(["three"]))
    assert second.count(cl.SECTION_START) == 1
    assert "- three" in second and "- one" not in second
    assert cl.upsert_pr_section(second, cl.render_pr_section(["three"])) == second
    assert cl.upsert_pr_section(None, first) == first
