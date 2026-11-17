import pytest

from boltons.mathutils import ceil


@pytest.mark.parametrize("options", [[10, 4, 6], (6, 10, 4), {4, 6, 10}, [6, 4, 10, 4]])
def test_unsorted_options(options):
    assert ceil(3, options=options) == 4
    assert ceil(5, options=options) == 6
    assert ceil(10, options=options) == 10


def test_generator_options():
    assert ceil(3, options=(x for x in [6, 4])) == 4
    with pytest.raises(ValueError):
        ceil(11, options=(x for x in [6, 4]))
