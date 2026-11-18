from boltons.mathutils import ceil, clamp, floor

CSA = [1.5, 2.5, 4, 6, 10, 25, 35, 50]


def test_ceil_and_floor_between_options():
    assert ceil(3.5, options=CSA) == 4
    assert floor(3.5, options=CSA) == 2.5


def test_without_options():
    assert ceil(1.2) == 2
    assert floor(1.8) == 1
    assert clamp(7, 0, 5) == 5
