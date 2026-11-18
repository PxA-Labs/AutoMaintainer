"""Read-only tools of the ADK Reviewer (#525), independent of ADK.

``ReviewTools`` serves one review: a checkout of the pull request at its head
SHA (``root``), the PR diff split per file, and the verification output of
the iteration under review. Its four methods are what the model may call:

- ``get_diff(path=None, page=1)``: the diff, whole or for one file, in pages;
- ``read_file(path, start=1, end=None)``: numbered lines of a file at the head;
- ``search_code(query)``: the nearest chunks of the RAG index when one is
  given (retrieval on and the index at the PR head, #526), else ripgrep over
  the checkout; an index that fails or finds nothing falls back to ripgrep;
- ``get_verification_log()``: the end of this iteration's test output.

Everything the model sends is attacker-steerable (the diff and the files it
reads can carry injected instructions), so every argument is checked here,
not in the model's prompt:

- Paths are relative to the checkout. Absolute paths, drive letters, ``..``
  segments and NUL bytes are refused, the path is resolved to its real path
  (symlinks included) and refused unless it stays inside the checkout, and
  ``.git`` is never served (a clone's config can hold a credential).
- The search query goes to ripgrep after ``--``, as a fixed string unless
  ``regex`` is asked for, with ``--no-config`` (RIPGREP_CONFIG_PATH cannot add
  flags), without following symlinks and ignoring the repository's own
  ignore files (an attacker's ``.rgignore`` cannot hide a file).
- Every output is capped (``ToolLimits``).

A refused argument raises ``ToolRefused`` and a missing or failing ripgrep
``ToolUnavailable``; the ADK adapter turns both into a tool answer, so a
refusal costs the model a tool call and never ends the review.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # nosec B404 - ripgrep with a fixed argv, no shell
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Optional

__all__ = [
    "ReviewTools",
    "ToolLimits",
    "ToolRefused",
    "ToolUnavailable",
    "TRUNCATED",
    "TRUNCATED_HEAD",
]

logger = logging.getLogger(__name__)

TRUNCATED = "\n...[truncated]"
TRUNCATED_HEAD = "...[earlier output truncated]\n"
MAX_QUERY_CHARS = 200
# Files larger than this are refused rather than read and then cut.
MAX_FILE_BYTES = 2_000_000


class ToolRefused(ValueError):
    """The tool call's arguments are not allowed (or name nothing)."""


class ToolUnavailable(RuntimeError):
    """The tool could not run (ripgrep missing, failed or timed out)."""


@dataclass(frozen=True)
class ToolLimits:
    """Output caps of one review's tools."""

    max_file_lines: int = 400
    max_output_chars: int = 12_000
    diff_page_chars: int = 8_000
    max_search_results: int = 50
    max_log_chars: int = 8_000
    search_timeout_seconds: float = 10.0


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _relative_parts(path) -> tuple[str, ...]:
    """``path`` as relative segments, or ToolRefused."""
    if not isinstance(path, str) or not path.strip():
        raise ToolRefused("a path relative to the repository root is required")
    if "\x00" in path:
        raise ToolRefused("the path contains a NUL byte")
    windows = PureWindowsPath(path)
    if windows.drive or windows.root or PurePosixPath(path).is_absolute():
        raise ToolRefused("absolute paths are not allowed; use a repository path")
    parts = tuple(p for p in path.replace("\\", "/").split("/") if p not in ("", "."))
    if not parts:
        raise ToolRefused("the path names the repository root, not a file")
    if ".." in parts:
        raise ToolRefused("'..' is not allowed: the path must stay in the repository")
    if any(p.lower() == ".git" for p in parts):
        raise ToolRefused("git metadata (.git) is not available")
    return parts


class ReviewTools:
    """The tools of one review, bound to the checkout at ``root``.

    ``diff_files`` is the PR diff as ``(path, section)`` pairs in PR order;
    ``verification_log`` the text of this iteration's verification.
    """

    def __init__(
        self,
        root,
        *,
        diff_files: Iterable[tuple[str, str]],
        verification_log: str,
        limits: ToolLimits = ToolLimits(),
        retriever=None,
        search_k: int = 8,
    ):
        real_root = Path(os.path.realpath(root))
        if not real_root.is_dir():
            raise ValueError(f"the review checkout {root} does not exist")
        self.root = real_root
        self.diff_files = [(str(p), str(s)) for p, s in diff_files]
        self.verification_log = verification_log or ""
        self.limits = limits
        # The RAG index of this repository at the PR head (#526), or None.
        # Bound to its tenant and commit by whoever built it; only the query
        # reaches it from the model.
        self.retriever = retriever
        self.search_k = max(1, int(search_k))

    # --- paths -------------------------------------------------------------------

    def resolve(self, path: str) -> Path:
        """The real path of ``path`` inside the checkout, or ToolRefused."""
        parts = _relative_parts(path)
        real = Path(os.path.realpath(self.root.joinpath(*parts)))
        if real == self.root or not real.is_relative_to(self.root):
            raise ToolRefused(f"{path!r} resolves outside the repository checkout")
        if any(p.lower() == ".git" for p in real.relative_to(self.root).parts):
            raise ToolRefused("git metadata (.git) is not available")
        return real

    # --- tools -------------------------------------------------------------------

    def read_file(self, path: str, start: int = 1, end: Optional[int] = None) -> str:
        """Lines ``start``..``end`` (1-based, inclusive) of ``path`` at the head."""
        real = self.resolve(path)
        if not real.exists():
            raise ToolRefused(f"no such file at the PR head: {path}")
        if not real.is_file():
            raise ToolRefused(f"{path} is not a file")
        if real.stat().st_size > MAX_FILE_BYTES:
            raise ToolRefused(
                f"{path} is too large to read ({MAX_FILE_BYTES} bytes max)"
            )
        data = real.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolRefused(f"{path} is a binary file")
        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        if not isinstance(start, int) or start < 1:
            raise ToolRefused("start must be a line number of at least 1")
        if end is not None and (not isinstance(end, int) or end < start):
            raise ToolRefused("end must be a line number not before start")
        if start > max(total, 1):
            raise ToolRefused(f"{path} has only {total} lines")
        last = min(total, end if end is not None else total)
        last = min(last, start + self.limits.max_file_lines - 1)
        shown = "\n".join(f"{n}| {lines[n - 1]}" for n in range(start, last + 1))
        header = f"{'/'.join(_relative_parts(path))} lines {start}-{last} of {total}"
        return _cap(header + "\n" + shown, self.limits.max_output_chars)

    def get_diff(self, path: Optional[str] = None, page: int = 1) -> str:
        """The PR diff (or the part for ``path``), page ``page`` of the result."""
        if path is None or (isinstance(path, str) and not path.strip()):
            text = "\n\n".join(section for _, section in self.diff_files)
            text = text or "(empty diff)"
        else:
            wanted = "/".join(_relative_parts(path))
            sections = [s for p, s in self.diff_files if p.replace("\\", "/") == wanted]
            if not sections:
                return f"(no changes to {wanted} in this pull request)"
            text = "\n\n".join(sections)
        size = max(self.limits.diff_page_chars, 1)
        pages = max(1, -(-len(text) // size))
        if not isinstance(page, int) or not 1 <= page <= pages:
            raise ToolRefused(f"page must be between 1 and {pages}")
        chunk = text[(page - 1) * size : page * size]
        return f"page {page} of {pages}\n{chunk}"

    def get_verification_log(self) -> str:
        """The end of this iteration's verification output."""
        log = self.verification_log
        if not log.strip():
            return "(no verification output)"
        limit = self.limits.max_log_chars
        if len(log) > limit:
            return TRUNCATED_HEAD + log[-limit:]
        return log

    def search_argv(self, query: str, *, rg: str, regex: bool = False) -> list[str]:
        """The ripgrep command line; the query is always after ``--``."""
        argv = [
            rg,
            "--no-config",
            "--line-number",
            "--no-heading",
            "--color=never",
            "--hidden",
            "--no-ignore",
            "--glob=!.git",
            "--max-count=20",
            "--max-filesize=1M",
            "--max-columns=300",
        ]
        if not regex:
            argv.append("--fixed-strings")
        return argv + ["--", query, "."]

    def search_code(self, query: str, regex: bool = False) -> str:
        """Code relevant to ``query``: the nearest chunks of the RAG index
        when there is one (and the search is not a regex), else lines that
        match in the checkout, as ``path:line:text`` (ripgrep).

        An index that fails or finds nothing falls back to ripgrep, so the
        tool answers as it did before retrieval whenever the index cannot.
        """
        if not isinstance(query, str) or not query:
            raise ToolRefused("a non-empty query is required")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolRefused(f"the query is longer than {MAX_QUERY_CHARS} characters")
        if any(c in query for c in "\x00\r\n"):
            raise ToolRefused("the query must be a single line")
        if self.retriever is not None and not regex:
            found = self._index_search(query)
            if found is not None:
                return found
        return self._ripgrep(query, regex=bool(regex))

    def _index_search(self, query: str) -> Optional[str]:
        """The index's nearest chunks with numbered lines, or None to fall
        back (an error, logged by type only, or no result)."""
        k = min(self.search_k, self.limits.max_search_results)
        try:
            chunks = list(self.retriever.retrieve(query, k))
        except Exception as e:  # the index is an optimisation, never required
            logger.warning(
                "search_code: the code index failed (%s); using ripgrep",
                type(e).__name__,
            )
            return None
        if not chunks:
            return None
        parts = [f"{len(chunks)} code index matches for {query!r}, best first"]
        for c in chunks:
            label = f"{c.path} lines {c.start_line}-{c.end_line}"
            if c.symbol:
                label += f" ({c.symbol})"
            lines = str(c.content).split("\n")
            numbered = "\n".join(
                f"{c.start_line + i}| {line}" for i, line in enumerate(lines)
            )
            parts.append(f"{label}\n{numbered}")
        return _cap("\n\n".join(parts), self.limits.max_output_chars)

    def _ripgrep(self, query: str, regex: bool = False) -> str:
        rg = shutil.which("rg")
        if rg is None:
            raise ToolUnavailable(
                "code search is unavailable: ripgrep is not installed"
            )
        env = {"PATH": os.environ.get("PATH", "")}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv, no shell
                self.search_argv(query, rg=rg, regex=bool(regex)),
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.limits.search_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolUnavailable("code search timed out") from None
        except OSError as e:
            raise ToolUnavailable(f"code search failed: {type(e).__name__}") from None
        if proc.returncode == 1:
            return f"(no matches for {query!r})"
        if proc.returncode != 0:
            raise ToolUnavailable("code search failed (invalid pattern?)")
        lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
        lines = [line[2:] if line[:2] in ("./", ".\\") else line for line in lines]
        shown = "\n".join(lines[: self.limits.max_search_results])
        if len(lines) > self.limits.max_search_results:
            shown += TRUNCATED
        return _cap(shown, self.limits.max_output_chars)
