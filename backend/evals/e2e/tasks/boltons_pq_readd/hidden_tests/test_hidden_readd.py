import pytest

from boltons.queueutils import HeapPriorityQueue, PriorityQueue


@pytest.mark.parametrize("cls", [PriorityQueue, HeapPriorityQueue])
def test_readd_changes_priority(cls):
    pq = cls()
    pq.add("a", 1)
    pq.add("b", 2)
    pq.add("a", 3)
    assert len(pq) == 2
    assert [pq.pop(), pq.pop()] == ["a", "b"]
    assert len(pq) == 0
    with pytest.raises(IndexError):
        pq.pop()


@pytest.mark.parametrize("cls", [PriorityQueue, HeapPriorityQueue])
def test_readd_lower_priority(cls):
    pq = cls()
    pq.add("a", 5)
    pq.add("b", 3)
    pq.add("a", 1)
    assert pq.peek() == "b"
    assert [pq.pop(), pq.pop(), pq.pop(default="done")] == ["b", "a", "done"]
