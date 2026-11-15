from pkgtools.versions import compare_versions


def test_multi_digit_parts_compare_numerically():
    assert compare_versions("1.10.0", "1.9.0") == 1
    assert compare_versions("0.9.9", "0.10") == -1


def test_missing_part_counts_as_zero():
    assert compare_versions("1.2", "1.2.0") == 0


def test_shorter_version_is_older_when_extra_part_is_positive():
    assert compare_versions("1.2", "1.2.1") == -1
