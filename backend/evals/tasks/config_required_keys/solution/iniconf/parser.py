"""A tiny parser for ``key = value`` configuration files."""


class ConfigError(ValueError):
    """The configuration text is invalid."""

    def __init__(self, message: str, missing=None):
        super().__init__(message)
        self.missing = list(missing or [])


def parse_config(text: str, *, required=()) -> dict[str, str]:
    """Parse ``key = value`` lines; blank lines and ``#`` comments are ignored.

    Raises ``ConfigError`` (with ``missing`` set) when a ``required`` key is
    absent.
    """
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"line {number}: expected 'key = value'")
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    missing = sorted(set(required) - set(result))
    if missing:
        raise ConfigError(f"missing required keys: {', '.join(missing)}", missing)
    return result
