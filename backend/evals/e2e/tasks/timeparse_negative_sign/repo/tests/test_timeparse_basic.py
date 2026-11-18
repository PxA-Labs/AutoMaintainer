from pytimeparse.timeparse import timeparse


def test_units():
    assert timeparse("1m24s") == 84
    assert timeparse("1.2 minutes") == 72


def test_clock_format():
    assert timeparse("1:24") == 84
    assert timeparse("garbage") is None
