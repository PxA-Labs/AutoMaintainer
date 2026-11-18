import pytest

from jsonpointer import JsonPointerException, resolve_pointer


@pytest.mark.parametrize("pointer", ["/01", "/00", "/007", "/0x1"])
def test_leading_zero_index_is_rejected(pointer):
    with pytest.raises(JsonPointerException):
        resolve_pointer(list(range(20)), pointer)


@pytest.mark.parametrize("pointer, expected", [("/0", 0), ("/1", 1), ("/10", 10)])
def test_valid_indices_still_resolve(pointer, expected):
    assert resolve_pointer(list(range(20)), pointer) == expected
