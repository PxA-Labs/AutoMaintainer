from structures.minstack import MinStack


def test_push_pop_order_and_len():
    stack = MinStack()
    for value in (4, 2, 9):
        stack.push(value)
    assert len(stack) == 3
    assert stack.pop() == 9
    assert len(stack) == 2


def test_min_after_pushes():
    stack = MinStack()
    for value in (4, 2, 9):
        stack.push(value)
    assert stack.min() == 2
