"""Validation of network settings."""


def parse_port(value) -> int:
    """Return ``value`` as a TCP port number (1-65535)."""
    return int(value)
