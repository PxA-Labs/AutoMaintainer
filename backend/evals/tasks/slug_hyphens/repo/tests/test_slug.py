from textutil.slug import make_slug


def test_spaces_become_hyphens():
    assert make_slug("Hello World") == "hello-world"


def test_digits_are_kept():
    assert make_slug("Release 2") == "release-2"
