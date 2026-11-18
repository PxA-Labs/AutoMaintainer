import pytest

from toolz.dicttoolz import get_in


@pytest.mark.parametrize(
    "keys, coll",
    [
        (["a", "b"], {"a": 1}),
        (["items", "first"], {"items": [1, 2]}),
        ([0, "x"], [[1]]),
        (["a", 0, "b"], {"a": [None]}),
    ],
)
def test_untraversable_path_gives_default(keys, coll):
    assert get_in(keys, coll) is None
    assert get_in(keys, coll, default=7) == 7


def test_no_default_still_raises():
    with pytest.raises(TypeError):
        get_in(["a", "b"], {"a": 1}, no_default=True)
