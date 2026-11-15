import pytest

from units.temperature import celsius_to_fahrenheit, fahrenheit_to_celsius


@pytest.mark.parametrize(
    "fahrenheit, celsius", [(212, 100), (32, 0), (-40, -40), (98.6, 37)]
)
def test_known_points(fahrenheit, celsius):
    assert fahrenheit_to_celsius(fahrenheit) == pytest.approx(celsius)


@pytest.mark.parametrize("celsius", [-273.15, -12.5, 0, 21, 1000])
def test_round_trip(celsius):
    assert fahrenheit_to_celsius(celsius_to_fahrenheit(celsius)) == pytest.approx(
        celsius
    )
