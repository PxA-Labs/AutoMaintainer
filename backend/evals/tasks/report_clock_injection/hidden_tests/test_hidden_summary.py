import re
from datetime import datetime

from reports.summary import render_report


def test_now_is_used_for_the_header():
    text = render_report("Daily", [("a", 1)], now=datetime(2031, 4, 5, 6, 7, 8))
    assert text.splitlines()[:3] == ["Daily", "Generated 2031-04-05 06:07", ""]


def test_output_is_reproducible_with_now():
    when = datetime(1999, 12, 31, 23, 59)
    rows = [("x", 5), ("y", 7)]
    assert render_report("R", rows, now=when) == render_report("R", rows, now=when)
    assert render_report("R", rows, now=when).splitlines()[-1] == (
        "Total                   12"
    )


def test_none_means_current_time():
    before = datetime.now().replace(second=0, microsecond=0)
    header = render_report("T", [], now=None).splitlines()[1]
    after = datetime.now()
    assert re.fullmatch(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}", header)
    shown = datetime.strptime(header, "Generated %Y-%m-%d %H:%M")
    assert before <= shown <= after
