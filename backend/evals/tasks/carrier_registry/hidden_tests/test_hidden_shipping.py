import uuid

import pytest

from logistics.shipping import register_carrier, shipping_cost


def _name():
    return "carrier-" + uuid.uuid4().hex


def test_registered_carrier_is_used():
    name = _name()
    register_carrier(name, lambda kg: 2.0 * kg + 1)
    assert shipping_cost(name, 3) == 7.0


def test_duplicate_name_requires_replace():
    name = _name()
    register_carrier(name, lambda kg: 1.0)
    with pytest.raises(ValueError):
        register_carrier(name, lambda kg: 2.0)
    assert shipping_cost(name, 1) == 1.0
    register_carrier(name, lambda kg: 2.0, replace=True)
    assert shipping_cost(name, 1) == 2.0


def test_builtin_carriers_are_registered():
    for name in ("post", "courier", "freight"):
        with pytest.raises(ValueError):
            register_carrier(name, lambda kg: 0.0)
    assert shipping_cost("courier", 0) == 9.0


def test_negative_weight_checked_before_rate_function():
    calls = []
    name = _name()
    register_carrier(name, lambda kg: calls.append(kg) or 0.0)
    with pytest.raises(ValueError):
        shipping_cost(name, -1)
    assert calls == []


def test_unknown_carrier_still_value_error():
    with pytest.raises(ValueError):
        shipping_cost(_name(), 1)
