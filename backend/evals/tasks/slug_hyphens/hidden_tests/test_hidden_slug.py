from textutil.slug import make_slug


def test_punctuation_runs_collapse_to_one_hyphen():
    assert make_slug("Hello,  World!") == "hello-world"


def test_no_leading_or_trailing_hyphens():
    assert make_slug("  --Rust & Go--  ") == "rust-go"


def test_only_punctuation_gives_empty_slug():
    assert make_slug("?!") == ""
