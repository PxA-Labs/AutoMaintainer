"""Application settings with environment overrides."""

import os

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


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
        if isinstance(default, bool):
            settings[name] = _parse_bool(raw)
        else:
            settings[name] = type(default)(raw)
    return settings
