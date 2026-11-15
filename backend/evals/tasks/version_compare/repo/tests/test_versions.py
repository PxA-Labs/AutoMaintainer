from pkgtools.versions import compare_versions


def test_patch_bump_is_newer():
    assert compare_versions("1.2.3", "1.2.4") == -1


def test_equal_versions():
    assert compare_versions("3.0.1", "3.0.1") == 0


def test_major_bump_is_newer():
    assert compare_versions("2.0.0", "1.9.9") == 1
