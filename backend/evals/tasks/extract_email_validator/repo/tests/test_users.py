import pytest

from accounts.users import change_email, create_user


def test_create_user_normalises_email():
    assert create_user(" Ada ", " Ada@Example.ORG ") == {
        "name": "Ada",
        "email": "ada@example.org",
    }


def test_change_email_rejects_garbage():
    with pytest.raises(ValueError):
        change_email({"name": "x", "email": "x@y.z"}, "no-at-sign")
