"""User account records."""


def validate_email(email: str) -> str:
    """Return ``email`` stripped and lower-cased, or raise ``ValueError``."""
    email = email.strip().lower()
    if email.count("@") != 1:
        raise ValueError(f"invalid email address: {email!r}")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith("."):
        raise ValueError(f"invalid email address: {email!r}")
    return email


def create_user(name: str, email: str) -> dict:
    """Return a new user record."""
    return {"name": name.strip(), "email": validate_email(email)}


def change_email(user: dict, email: str) -> dict:
    """Return a copy of ``user`` with a new email address."""
    return {**user, "email": validate_email(email)}
