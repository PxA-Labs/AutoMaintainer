from collections import OrderedDict

from toolz.dicttoolz import assoc_in, update_in


def test_input_is_not_mutated():
    d = {"a": 1, "b": {"c": 2}}
    assert update_in(d, ["a"], lambda x: x + 1) == {"a": 2, "b": {"c": 2}}
    assert update_in(d, ["b", "c"], lambda x: x * 10) == {"a": 1, "b": {"c": 20}}
    assert update_in(d, ["new"], str, default=5) == {"a": 1, "b": {"c": 2}, "new": "5"}
    assert d == {"a": 1, "b": {"c": 2}}


def test_assoc_in_does_not_mutate():
    d = {"x": {"y": 1}}
    assert assoc_in(d, ["x", "z"], 3) == {"x": {"y": 1, "z": 3}}
    assert assoc_in(d, ["w"], 0) == {"x": {"y": 1}, "w": 0}
    assert d == {"x": {"y": 1}}


def test_factory_is_used_for_the_result():
    result = update_in({"a": 1}, ["a"], str, factory=OrderedDict)
    assert isinstance(result, OrderedDict)
    assert result == {"a": "1"}
