from reports.summary import render_report


def test_rows_and_total():
    text = render_report("Signups", [("web", 12), ("mobile", 30)])
    lines = text.splitlines()
    assert lines[0] == "Signups"
    assert lines[1].startswith("Generated ")
    assert lines[3:] == [
        "web                     12",
        "mobile                  30",
        "Total                   42",
    ]
