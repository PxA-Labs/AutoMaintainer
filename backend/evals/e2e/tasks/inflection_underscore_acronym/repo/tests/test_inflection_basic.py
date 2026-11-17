import inflection


def test_camelize_and_underscore():
    assert inflection.camelize("device_type") == "DeviceType"
    assert inflection.underscore("DeviceType") == "device_type"


def test_ordinalize_simple_numbers():
    assert inflection.ordinalize(1) == "1st"
    assert inflection.ordinalize(4) == "4th"


def test_parameterize_simple_name():
    assert inflection.parameterize("Donald E. Knuth") == "donald-e-knuth"
