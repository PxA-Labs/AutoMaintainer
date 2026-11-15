import pytest

from appconfig.settings import load_settings


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "Off"])
def test_false_values(raw):
    assert load_settings({"debug": True}, {"APP_DEBUG": raw}) == {"debug": False}


@pytest.mark.parametrize("raw", ["true", "True", "1", "YES", "on"])
def test_true_values(raw):
    assert load_settings({"debug": False}, {"APP_DEBUG": raw}) == {"debug": True}


def test_invalid_boolean_raises():
    with pytest.raises(ValueError):
        load_settings({"debug": False}, {"APP_DEBUG": "maybe"})


def test_int_still_converted():
    assert load_settings({"workers": 1}, {"APP_WORKERS": "4"}) == {"workers": 4}
