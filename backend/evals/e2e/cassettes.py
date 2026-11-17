"""Recorded model answers for replaying whole e2e runs offline.

One cassette per task (``pipeline``) or Reviewer case (``reviewer``)::

    {"kind", "id", "source", "synthetic", "model", "note",
     "runs": [[call, ...], ...]}     # one list of calls per repeat

    call = {"role", "prompt_sha256", "response", "finish_reason",
            "usage", "latency_ms"}

Replay answers the calls of ``runs[repeat % len(runs)]`` in order. Each call
stores the hash of the prompt it answered, after ``normalize_prompt``: the
Maintainer's prompt quotes the verification output, which holds timings,
temporary paths and the interpreter path, and those differ between machines
and runs without changing what the model was asked. A call whose normalised
prompt differs is counted as stale: the run still completes, but its numbers
do not describe the current prompts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

E2E_CASSETTES_DIR = Path(__file__).resolve().parent / "cassettes"
FIXTURES_DIR = E2E_CASSETTES_DIR / "fixtures"
KINDS = ("pipeline", "reviewer")

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?s\b")
_HOST_PATH_RE = re.compile(r"<host>[^\s'\"]*")
_VERIFICATION_BLOCK_RE = re.compile(
    r'<untrusted-data source="verification output">\n.*?\n</untrusted-data>',
    re.DOTALL,
)


def _volatile_prefixes() -> list[str]:
    prefixes = {sys.executable, os.environ.get("VERIFY_PYTHON") or ""}
    tmp = tempfile.gettempdir()
    prefixes |= {tmp, tmp.replace("\\", "/"), os.path.realpath(tmp)}
    return sorted((p for p in prefixes if p), key=len, reverse=True)


def normalize_prompt(text: str) -> str:
    """``text`` without the parts that vary between equivalent runs.

    The verification block (command, summary and pytest output of the
    engine's test run) is replaced as a whole: it is data from the host's
    test run, worded differently by other pytest versions and platforms,
    not part of the prompt the model is given. The status line before it
    (``status=passed``) stays.
    """
    text = _VERIFICATION_BLOCK_RE.sub("<verification output>", text)
    for prefix in _volatile_prefixes():
        text = text.replace(prefix, "<host>")
    # The rest of a temporary path (a random directory name) varies too.
    text = _HOST_PATH_RE.sub("<host>", text)
    text = text.replace("\r\n", "\n")
    text = _SHA_RE.sub("<sha>", text)
    return _DURATION_RE.sub("<t>", text)


def prompt_hash(system_prompt: str, user_prompt: str) -> str:
    joined = f"{normalize_prompt(system_prompt)}\0{normalize_prompt(user_prompt)}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _cassette(kind, case_id, runs, source, model=None, note=""):
    return {
        "kind": kind,
        "id": case_id,
        "source": source,
        "synthetic": source != "live",
        "model": model or f"{source}-policy",
        "note": note,
        "runs": [[dict(call) for call in run] for run in runs],
    }


def pipeline_cassette(task_id, runs, source, model=None, note=""):
    return _cassette("pipeline", task_id, runs, source, model, note)


def reviewer_cassette(case_id, runs, source, model=None, note=""):
    return _cassette("reviewer", case_id, runs, source, model, note)


def path_for(directory: Path, kind: str, case_id: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"cassette kind must be one of {KINDS}")
    return Path(directory) / kind / f"{case_id}.json"


def save(directory: Path, cassette: dict) -> Path:
    path = path_for(directory, cassette["kind"], cassette["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cassette, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_for(directory: Path, kind: str, case_id: str) -> dict:
    path = path_for(directory, kind, case_id)
    if not path.is_file():
        raise FileNotFoundError(f"no {kind} cassette for {case_id!r} at {path}")
    return load(path)
