import pytest

from boltons.mathutils import floor

OPTIONS = [1.5, 2.5, 4, 6, 10]


@pytest.mark.parametrize("x", [1.5, 2.5, 4, 6, 10])
def test_exact_option_is_returned(x):
    assert floor(x, options=OPTIONS) == x


def test_only_values_below_every_option_raise():
    assert floor(11, options=OPTIONS) == 10
    with pytest.raises(ValueError):
        floor(1.4, options=OPTIONS)
