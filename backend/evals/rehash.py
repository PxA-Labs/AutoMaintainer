"""Re-bind SYNTHETIC cassettes to the current Implementer prompt.

Usage (from backend/)::

    python -m evals.rehash [--cassettes evals/cassettes/synthetic]

The hand-written cassettes in ``cassettes/synthetic/`` test the harness and
``normalize_generated_code``, not the model, so after an intentional prompt
change their responses stay valid and only ``prompt_sha256`` /
``prompt_version`` need updating (CI's replay step runs ``--fail-on-stale``).

Cassettes recorded from a real model are refused: their responses answered the
old prompt, so they must be re-recorded with ``--mode live --record``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .llm import CASSETTES_DIR
from .prompt import PROMPT_VERSION, build_implementer_prompt, prompt_sha256
from .taskset import TASKS_DIR, load_tasks


def rehash(cassettes_dir: Path, tasks_dir: Path = TASKS_DIR) -> tuple[list, list]:
    """Return ``(updated task ids, refused real-model task ids)``."""
    updated, refused = [], []
    for task in load_tasks(tasks_dir):
        path = cassettes_dir / f"{task.id}.json"
        if not path.is_file():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if not record.get("synthetic"):
            refused.append(task.id)
            continue
        digest = prompt_sha256(
            *build_implementer_prompt(
                task.issue_text, task.target_file, task.read_target()
            )
        )
        if (record.get("prompt_sha256"), record.get("prompt_version")) == (
            digest,
            PROMPT_VERSION,
        ):
            continue
        record.update(prompt_sha256=digest, prompt_version=PROMPT_VERSION)
        path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        updated.append(task.id)
    return updated, refused


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.rehash", description=__doc__)
    p.add_argument("--cassettes", type=Path, default=CASSETTES_DIR)
    p.add_argument("--tasks-dir", type=Path, default=TASKS_DIR)
    args = p.parse_args(argv)
    updated, refused = rehash(args.cassettes, args.tasks_dir)
    print(f"re-hashed {len(updated)} synthetic cassette(s): {updated}")
    if refused:
        print(
            f"refused real-model cassettes {refused}: re-record them with "
            "--mode live --record",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
