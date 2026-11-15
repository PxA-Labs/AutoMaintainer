from signals.bus import EventBus


def test_unsubscribe_stops_delivery_and_is_idempotent():
    bus, seen = EventBus(), []
    off = bus.subscribe("e", seen.append)
    bus.subscribe("e", lambda p: seen.append(p * 10))
    assert bus.publish("e", 1) == 2
    off()
    off()
    assert bus.publish("e", 2) == 1
    assert seen == [1, 10, 20]


def test_same_handler_twice_removes_only_one_subscription():
    bus, seen = EventBus(), []
    first = bus.subscribe("e", seen.append)
    bus.subscribe("e", seen.append)
    first()
    assert bus.publish("e", "x") == 1
    assert seen == ["x"]


def test_self_unsubscribe_during_publish_skips_nobody():
    bus, seen = EventBus(), []
    offs = {}

    def once(payload):
        seen.append(("once", payload))
        offs["once"]()

    offs["once"] = bus.subscribe("e", once)
    bus.subscribe("e", lambda p: seen.append(("always", p)))
    assert bus.publish("e", 1) == 2
    assert bus.publish("e", 2) == 1
    assert seen == [("once", 1), ("always", 1), ("always", 2)]


def test_unsubscribing_another_handler_mid_publish():
    bus, seen = EventBus(), []
    offs = {}
    bus.subscribe("e", lambda p: offs["b"]())
    offs["b"] = bus.subscribe("e", lambda p: seen.append("b"))
    assert bus.publish("e") == 2
    assert seen == ["b"]
    assert bus.publish("e") == 1
    assert seen == ["b"]


def test_unsubscribe_of_one_event_leaves_others():
    bus, seen = EventBus(), []
    off = bus.subscribe("a", seen.append)
    bus.subscribe("b", seen.append)
    off()
    assert bus.publish("a", 1) == 0
    assert bus.publish("b", 2) == 1
    assert seen == [2]
