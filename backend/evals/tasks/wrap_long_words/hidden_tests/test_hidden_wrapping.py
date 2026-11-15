import pytest

from textutil.wrapping import wrap


def test_word_of_exact_width_first():
    assert wrap("abcd ef", 4) == ["abcd", "ef"]


def test_overlong_word_gets_its_own_line():
    assert wrap("abcdefghij", 4) == ["abcdefghij"]
    assert wrap("ab abcdefghij cd", 5) == ["ab", "abcdefghij", "cd"]


def test_whitespace_runs_collapse():
    assert wrap("  ab   cd\tef\n gh ", 5) == ["ab cd", "ef gh"]


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text(text):
    assert wrap(text, 10) == []


@pytest.mark.parametrize("width", [0, -3])
def test_invalid_width(width):
    with pytest.raises(ValueError):
        wrap("a b", width)


def test_lines_respect_width_and_keep_words():
    text = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do"
    lines = wrap(text, 12)
    assert all(len(line) <= 12 for line in lines)
    assert " ".join(lines).split() == text.split()
    assert lines[0] == "lorem ipsum"
