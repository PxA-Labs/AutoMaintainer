from boltons.queueutils import HeapPriorityQueue, PriorityQueue


def test_higher_priority_first():
    for cls in (PriorityQueue, HeapPriorityQueue):
        pq = cls()
        pq.add("low", 1)
        pq.add("high", 5)
        assert pq.pop() == "high"
        assert pq.pop() == "low"


def test_empty_pop_default():
    assert PriorityQueue().pop(default=None) is None
