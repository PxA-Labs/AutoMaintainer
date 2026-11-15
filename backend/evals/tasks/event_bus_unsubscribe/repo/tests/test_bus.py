from signals.bus import EventBus


def test_publish_calls_handlers_in_order():
    bus, seen = EventBus(), []
    bus.subscribe("saved", lambda p: seen.append(("a", p)))
    bus.subscribe("saved", lambda p: seen.append(("b", p)))
    assert bus.publish("saved", 1) == 2
    assert seen == [("a", 1), ("b", 1)]


def test_publish_without_handlers():
    assert EventBus().publish("nothing") == 0
