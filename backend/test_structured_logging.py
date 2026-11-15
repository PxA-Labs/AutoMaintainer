"""Structured logging must format records from any logger (#404).

The trace filter used to sit on the root *logger*, which only runs for records
created on the root logger itself. A record from a child logger propagated to
the root handler without trace_id/span_id, the formatter raised KeyError, and
the record was dropped.
"""

import io
import logging
import sys

import pytest

import observability


@pytest.fixture
def root_with_structured_logging(monkeypatch):
    """Run setup_structured_logging against a clean root logger, then restore it."""
    root = logging.getLogger()
    saved_handlers, saved_filters, saved_level = (
        root.handlers[:],
        root.filters[:],
        root.level,
    )
    root.handlers, root.filters = [], []
    monkeypatch.setattr(observability, "OPENTELEMETRY_AVAILABLE", False)
    # StreamHandler() binds sys.stderr when created, so this captures both the
    # formatted records and any "--- Logging error ---" report.
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    try:
        observability.setup_structured_logging(logging.INFO)
        yield stream
    finally:
        root.handlers, root.filters = saved_handlers, saved_filters
        root.setLevel(saved_level)


def test_child_logger_record_is_formatted_with_trace_fields(
    root_with_structured_logging,
):
    child = logging.getLogger("x.y")
    child.info("hello from a child logger")

    out = root_with_structured_logging.getvalue()
    assert "Logging error" not in out
    assert "hello from a child logger" in out
    assert "trace_id=" + "0" * 32 in out
    assert "span_id=" + "0" * 16 in out


def test_setup_is_idempotent(root_with_structured_logging):
    observability.setup_structured_logging(logging.INFO)
    observability.setup_structured_logging(logging.INFO)
    structured = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_automaintainer_structured", False)
    ]
    assert len(structured) == 1


def test_formatter_defaults_cover_records_that_bypass_the_filter():
    """Even a handler without the filter cannot KeyError on the trace fields."""
    handler = observability.make_structured_handler(io.StringIO())
    record = logging.LogRecord("x.y", logging.INFO, __file__, 1, "msg", None, None)
    formatted = handler.formatter.format(record)
    assert "trace_id=" + "0" * 32 in formatted
