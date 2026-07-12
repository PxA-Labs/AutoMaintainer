### Description

The `Backend CI / lint-and-check` workflow is currently failing during the `Check code formatting with Black` step. This failure is blocking pull requests and release processes.

From analyzing the workflow run logs (Run ID: `27874267501`), the `black --check .` command exits with code `1` because the following files are not styled according to Black's formatting rules:
- `backend/main.py`
- `backend/agents.py`

When the CI environment runs `black --check .`, it outputs:
```
would reformat /home/runner/work/AutoMaintainer/AutoMaintainer/backend/main.py
would reformat /home/runner/work/AutoMaintainer/AutoMaintainer/backend/agents.py

Oh no! 💥 💔 💥
2 files would be reformatted, 4 files would be left unchanged.
##[error]Process completed with exit code 1.
```

### Proposed Fix

1. **Format Files Locally:**
   Format the offending files using `black` from the root or the backend directory:
   ```bash
   black backend/main.py backend/agents.py
   ```
2. **Local Lint Validation:**
   Before staging and committing, run the local check to ensure compliance:
   ```bash
   black --check .
   ```
3. **Prevention (Optional but Recommended):**
   Add a standard pre-commit hook configuration or document a reminder in `CONTRIBUTING.md` to run `black .` prior to opening a PR.

### Acceptance Criteria

1. **Formatting Compliance:** The command `black --check .` must run successfully on the codebase and return exit code `0` with the message `all files would be left unchanged`.
2. **CI Pipeline Pass:** The `Backend CI` workflow must run to completion and show a successful green tick for the `lint-and-check` job on PR and push events.
3. **Professionalism Constraints:** No emojis should be used in any of the codebase changes, commits, or PR descriptions.

### Acceptance Approach

When reviewing the Pull Request that addresses this issue:
1. **Local Formatting Run:** Run `black --check .` on the branch to confirm that no files need formatting.
2. **Review CI Run:** Confirm that the GitHub Actions run triggered by the PR successfully builds and passes all steps (including Black formatting and flake8 linting) in the `lint-and-check` job.
