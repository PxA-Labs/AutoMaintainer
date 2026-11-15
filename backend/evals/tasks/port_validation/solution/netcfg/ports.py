"""Validation of network settings."""


def parse_port(value) -> int:
    """Return ``value`` as a TCP port number (1-65535)."""
    if isinstance(value, bool):
        raise ValueError("a boolean is not a port")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"not a port number: {value!r}")
        port = int(text)
    else:
        raise ValueError(f"not a port number: {value!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return port
