import pytest

from structures.minstack import MinStack


def test_min_is_restored_after_pop():
    stack = MinStack()
    for value in (5, 3, 7, 1):
        stack.push(value)
    assert stack.min() == 1
    assert stack.pop() == 1
    assert stack.min() == 3
    stack.pop()
    stack.pop()
    assert stack.min() == 5


def test_duplicate_minimum():
    stack = MinStack()
    for value in (5, 3, 3):
        stack.push(value)
    stack.pop()
    assert stack.min() == 3
    stack.pop()
    assert stack.min() == 5


def test_empty_stack_raises_index_error():
    stack = MinStack()
    with pytest.raises(IndexError):
        stack.min()
    with pytest.raises(IndexError):
        stack.pop()
    stack.push(1)
    stack.pop()
    with pytest.raises(IndexError):
        stack.min()


def test_push_after_emptying():
    stack = MinStack()
    stack.push(1)
    stack.pop()
    stack.push(8)
    assert stack.min() == 8
