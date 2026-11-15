"""Application settings with environment overrides."""

import os


def load_settings(defaults: dict, environ=None, prefix: str = "APP_") -> dict:
    """Return ``defaults`` updated with overrides from the environment.

    A default named ``port`` is overridden by ``APP_PORT``. The override is
    converted to the type of the default value.
    """
    environ = os.environ if environ is None else environ
    settings = dict(defaults)
    for name, default in defaults.items():
        raw = environ.get(prefix + name.upper())
        if raw is None:
            continue
        settings[name] = type(default)(raw)
    return settings
