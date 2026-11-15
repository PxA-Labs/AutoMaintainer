import pytest

from logistics.shipping import shipping_cost


@pytest.mark.parametrize(
    "carrier, weight, expected",
    [
        ("post", 1, 4.5),
        ("post", 5, 8.1),
        ("courier", 10, 17.0),
        ("freight", 10, 60.0),
        ("freight", 1000, 350.0),
    ],
)
def test_existing_carriers(carrier, weight, expected):
    assert shipping_cost(carrier, weight) == pytest.approx(expected)


def test_unknown_carrier():
    with pytest.raises(ValueError):
        shipping_cost("pigeon", 1)
