from resilience.retry import retry


def test_returns_value_on_first_success():
    @retry(times=3)
    def answer(x):
        return x * 2

    assert answer(21) == 42


def test_keeps_function_name():
    @retry()
    def fetch_profile():
        return 1

    assert fetch_profile.__name__ == "fetch_profile"
