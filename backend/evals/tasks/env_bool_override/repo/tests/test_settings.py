from appconfig.settings import load_settings


def test_int_override_is_converted():
    assert load_settings({"port": 80}, {"APP_PORT": "8080"}) == {"port": 8080}


def test_missing_override_keeps_default():
    assert load_settings({"name": "demo"}, {}) == {"name": "demo"}


def test_custom_prefix():
    assert load_settings({"name": "x"}, {"SVC_NAME": "y"}, prefix="SVC_") == {
        "name": "y"
    }
