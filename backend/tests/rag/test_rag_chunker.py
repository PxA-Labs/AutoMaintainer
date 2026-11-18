"""Function-level chunking of a repository checkout (#526)."""

import hashlib
import textwrap

import pytest

from automaintainer._runtime import retrieval
from automaintainer._runtime.retrieval import CodeChunk, chunk_file, chunk_repository

SAMPLE = textwrap.dedent('''\
    """Module docstring."""
    import os

    LIMIT = 3


    class Queue:
        """A queue."""

        size = 0

        def add(self, item):
            self.size += 1
            return item

        @property
        def empty(self):
            return self.size == 0


    def helper(x):
        def inner(y):
            return y

        return inner(x)
    ''')


def _by_symbol(chunks):
    return {c.symbol: c for c in chunks}


def test_python_file_is_chunked_per_function_method_and_class(tmp_path):
    (tmp_path / "q.py").write_text(SAMPLE, encoding="utf-8")
    chunks = chunk_file(tmp_path, "q.py")
    by = _by_symbol(chunks)
    assert set(by) == {"Queue", "Queue.add", "Queue.empty", "helper"}
    assert all(c.path == "q.py" for c in chunks)

    add = by["Queue.add"]
    assert (add.start_line, add.end_line) == (12, 14)
    assert add.content.startswith("    def add(self, item):")

    # Decorators belong to the function they decorate.
    assert by["Queue.empty"].content.lstrip().startswith("@property")

    # The class chunk is its header (docstring, attributes), not its methods
    # again: method bodies are not embedded twice.
    header = by["Queue"]
    assert header.start_line == 7
    assert '"""A queue."""' in header.content and "size = 0" in header.content
    assert "def add" not in header.content

    # Nested functions stay inside their parent's chunk.
    assert "def inner" in by["helper"].content


def test_chunk_content_matches_the_file_lines(tmp_path):
    (tmp_path / "q.py").write_text(SAMPLE, encoding="utf-8")
    lines = SAMPLE.splitlines()
    for chunk in chunk_file(tmp_path, "q.py"):
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def test_content_sha_covers_path_symbol_and_content():
    a = CodeChunk("a.py", "f", 1, 2, "def f(): pass")
    assert a.content_sha == hashlib.sha256(a.embedding_text.encode()).hexdigest()
    assert "a.py" in a.embedding_text and "def f(): pass" in a.embedding_text
    assert a.content_sha != CodeChunk("b.py", "f", 1, 2, "def f(): pass").content_sha
    assert a.content_sha == CodeChunk("a.py", "f", 9, 10, "def f(): pass").content_sha


def test_long_functions_are_split_into_capped_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_CHUNK_LINES", 10)
    body = "\n".join(f"    x{i} = {i}" for i in range(25))
    (tmp_path / "big.py").write_text(f"def big():\n{body}\n", encoding="utf-8")
    chunks = chunk_file(tmp_path, "big.py")
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 10), (11, 20), (21, 26)]
    assert {c.symbol for c in chunks} == {"big"}


def test_long_lines_are_capped_in_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_CHUNK_CHARS", 100)
    (tmp_path / "wide.py").write_text(
        "def wide():\n    return '" + "a" * 500 + "'\n", encoding="utf-8"
    )
    (chunk,) = chunk_file(tmp_path, "wide.py")
    assert len(chunk.content) <= 100 + len(retrieval.TRUNCATED_MARKER)
    assert chunk.content.endswith(retrieval.TRUNCATED_MARKER)


def test_python_file_without_definitions_falls_back_to_the_whole_file(tmp_path):
    (tmp_path / "settings.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
    (chunk,) = chunk_file(tmp_path, "settings.py")
    assert (chunk.symbol, chunk.start_line, chunk.end_line) == (None, 1, 2)


def test_non_python_text_files_get_file_level_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_CHUNK_LINES", 3)
    (tmp_path / "README.md").write_text("# T\n\nline\nmore\nend\n", encoding="utf-8")
    chunks = chunk_file(tmp_path, "README.md")
    assert [(c.symbol, c.start_line, c.end_line) for c in chunks] == [
        (None, 1, 3),
        (None, 4, 5),
    ]


@pytest.mark.parametrize(
    "name, data",
    [
        ("blob.bin", b"\x00\x01\x02binary"),
        ("latin.txt", "caf\xe9".encode("latin-1")),
        ("empty.txt", b""),
        ("package-lock.json", b'{"a": 1}'),
        ("app.min.js", b"var a=1;"),
    ],
)
def test_binary_empty_and_generated_files_are_skipped(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)
    assert chunk_file(tmp_path, name) == []


def test_oversized_files_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_FILE_BYTES", 10)
    (tmp_path / "a.txt").write_text("x" * 11, encoding="utf-8")
    assert chunk_file(tmp_path, "a.txt") == []


def test_repository_walk_skips_ignored_dirs_and_symlinks_and_is_sorted(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    for ignored in (".git", "node_modules", "__pycache__", ".venv"):
        (tmp_path / ignored).mkdir()
        (tmp_path / ignored / "x.py").write_text("def g(): pass\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def secret(): pass\n", encoding="utf-8")
    try:
        (tmp_path / "link.py").symlink_to(outside)
    except OSError:
        pass  # no symlink privilege (Windows): the rest still holds
    chunks = chunk_repository(tmp_path)
    assert [(c.path, c.symbol) for c in chunks] == [
        ("a.txt", None),
        ("pkg/m.py", "f"),
    ]


def test_chunk_file_refuses_paths_outside_the_repository(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")
    with pytest.raises(ValueError):
        chunk_file(tmp_path / "r", "../x.py")


def test_symbol_names_are_right_after_non_ascii_text(tmp_path):
    """ast_indexer slices names with byte offsets out of a str, so any
    multi-byte character above a definition shifts them ("terange(s" for
    "daterange"). The chunker takes the name from the definition line."""
    source = (
        '"""Café — naïve “quotes” → arrows."""\n'
        "\n"
        "class Week:\n"
        "    def day(self, n):\n"
        "        return n\n"
        "\n"
        "\n"
        "async def daterange(start, stop):\n"
        "    return start\n"
    )
    (tmp_path / "w.py").write_text(source, encoding="utf-8")
    symbols = {c.symbol for c in chunk_file(tmp_path, "w.py")}
    assert symbols == {"Week", "Week.day", "daterange"}


def test_javascript_definitions_are_named_from_their_line(tmp_path):
    source = (
        "// ünïcode\n"
        "class Box {\n"
        "  open(x) {\n"
        "    return x;\n"
        "  }\n"
        "}\n"
        "function wrap(y) {\n"
        "  return y;\n"
        "}\n"
    )
    (tmp_path / "b.js").write_text(source, encoding="utf-8")
    symbols = {c.symbol for c in chunk_file(tmp_path, "b.js")}
    assert symbols == {"Box", "Box.open", "wrap"}
