import pytest

from accounts import users


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" Bob@Mail.COM ", "bob@mail.com"),
        ("a.b@sub.example.org", "a.b@sub.example.org"),
    ],
)
def test_validate_email_normalises(raw, expected):
    assert users.validate_email(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "plain", "a@@b.c", "@b.c", "a@nodot", "a@.com", "a@b@c.d"]
)
def test_validate_email_rejects(raw):
    with pytest.raises(ValueError):
        users.validate_email(raw)


def test_both_entry_points_use_validate_email(monkeypatch):
    seen = []

    def strict(email):
        seen.append(email)
        return "checked@example.org"

    monkeypatch.setattr(users, "validate_email", strict)
    assert users.create_user("A", "first@x.io")["email"] == "checked@example.org"
    user = {"name": "A", "email": "old@x.io"}
    assert users.change_email(user, "second@x.io") == {
        "name": "A",
        "email": "checked@example.org",
    }
    assert seen == ["first@x.io", "second@x.io"]
