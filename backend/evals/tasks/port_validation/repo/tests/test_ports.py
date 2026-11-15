from netcfg.ports import parse_port


def test_int_port():
    assert parse_port(8080) == 8080


def test_string_port():
    assert parse_port("443") == 443
