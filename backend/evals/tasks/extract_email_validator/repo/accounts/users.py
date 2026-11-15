"""User account records."""


def create_user(name: str, email: str) -> dict:
    """Return a new user record."""
    email = email.strip().lower()
    if email.count("@") != 1:
        raise ValueError(f"invalid email address: {email!r}")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith("."):
        raise ValueError(f"invalid email address: {email!r}")
    return {"name": name.strip(), "email": email}


def change_email(user: dict, email: str) -> dict:
    """Return a copy of ``user`` with a new email address."""
    email = email.strip().lower()
    if email.count("@") != 1:
        raise ValueError(f"invalid email address: {email!r}")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith("."):
        raise ValueError(f"invalid email address: {email!r}")
    return {**user, "email": email}
