import pytest

from resilience.retry import retry


class Flaky:
    def __init__(self, failures, exc):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc(f"attempt {self.calls}")
        return "ok"


def test_succeeds_on_last_allowed_attempt():
    flaky = Flaky(2, ConnectionError)
    assert retry(times=3, exceptions=(ConnectionError,))(flaky)() == "ok"
    assert flaky.calls == 3


def test_reraises_last_error_after_all_attempts():
    flaky = Flaky(10, TimeoutError)
    wrapped = retry(times=4, exceptions=(TimeoutError,))(flaky)
    with pytest.raises(TimeoutError, match="attempt 4"):
        wrapped()
    assert flaky.calls == 4


def test_unlisted_exception_propagates_without_retry():
    flaky = Flaky(1, KeyError)
    wrapped = retry(times=5, exceptions=(ConnectionError,))(flaky)
    with pytest.raises(KeyError):
        wrapped()
    assert flaky.calls == 1


def test_subclasses_of_listed_exceptions_are_retried():
    flaky = Flaky(1, ConnectionResetError)
    assert retry(times=2, exceptions=(ConnectionError,))(flaky)() == "ok"
    assert flaky.calls == 2
