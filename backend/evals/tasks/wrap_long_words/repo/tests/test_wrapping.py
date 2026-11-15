from textutil.wrapping import wrap


def test_wraps_simple_sentence():
    assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]


def test_short_text_is_one_line():
    assert wrap("hi there", 80) == ["hi there"]
