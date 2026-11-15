import pytest

from evals.stats import mcnemar_exact_p, wilson_interval


@pytest.mark.parametrize(
    "k, n, expected",
    [
        # Reference values computed from the closed-form Wilson formula.
        (6, 10, (0.3127, 0.8318)),
        (0, 10, (0.0, 0.2775)),
        (10, 10, (0.7225, 1.0)),
        (1, 2, (0.0945, 0.9055)),
        (50, 100, (0.4038, 0.5962)),
    ],
)
def test_wilson_interval_reference_values(k, n, expected):
    lo, hi = wilson_interval(k, n)
    assert (round(lo, 4), round(hi, 4)) == expected


def test_wilson_interval_stays_in_unit_range_and_contains_estimate():
    for n in range(1, 30):
        for k in range(n + 1):
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= k / n <= hi <= 1.0


def test_wilson_interval_narrows_with_more_tasks():
    small = wilson_interval(5, 10)
    large = wilson_interval(50, 100)
    assert large[1] - large[0] < small[1] - small[0]


def test_wilson_interval_empty_and_invalid():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson_interval(3, 2)
    with pytest.raises(ValueError):
        wilson_interval(-1, 2)


@pytest.mark.parametrize(
    "b, c, expected",
    [
        (0, 0, 1.0),
        (1, 1, 1.0),
        (0, 5, 0.0625),  # 2 * (1/2)^5
        (5, 0, 0.0625),
        (1, 6, 0.125),  # 2 * (1 + 7) / 2^7
    ],
)
def test_mcnemar_exact_p(b, c, expected):
    assert mcnemar_exact_p(b, c) == pytest.approx(expected)
